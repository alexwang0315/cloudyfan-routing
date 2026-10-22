'use strict';
// Private JSON-line fixture control. Never export child output or stderr text.
const { spawn } = require('node:child_process');
const readline = require('node:readline');

function startFixture(command, args, { cwd, env, responseTimeoutMs = 15000 }) {
  const child = spawn(command, args, { cwd, env, stdio: ['pipe', 'pipe', 'pipe'] });
  const reader = readline.createInterface({ input: child.stdout });
  const queue = [], waiters = [];
  let failure, outputEnded = false, closed, stderrPresent = false;
  const exit = new Promise(resolve => child.once('close', (code, signal) => {
    closed = { code, signal };
    fail('Fixture process closed before another message');
    resolve(closed);
  }));

  function fail(message) {
    failure ||= Error(message);
    for (const waiter of waiters.splice(0)) {
      clearTimeout(waiter.timer);
      waiter.reject(failure);
    }
  }
  child.once('error', () => fail('Fixture process could not start'));
  child.stdin.on('error', () => fail('Fixture input pipe is unavailable'));
  child.stdout.on('error', () => fail('Fixture output pipe is unavailable'));
  child.stderr.on('data', () => { stderrPresent = true; });
  reader.on('line', line => {
    let value;
    try { value = JSON.parse(line); }
    catch { fail('Fixture output contained an invalid JSON message'); return; }
    if (failure) return;
    const waiter = waiters.shift();
    if (waiter) { clearTimeout(waiter.timer); waiter.resolve(value); }
    else queue.push(value);
  });
  reader.once('close', () => {
    outputEnded = true;
    fail('Fixture output closed before another message');
  });

  async function next() {
    if (queue.length) return queue.shift();
    if (failure) throw failure;
    if (closed || outputEnded) throw Error('Fixture output is closed');
    return new Promise((resolve, reject) => {
      const waiter = { resolve, reject };
      waiter.timer = setTimeout(() => {
        const index = waiters.indexOf(waiter);
        if (index !== -1) waiters.splice(index, 1);
        reject(Error('Fixture response timeout'));
      }, responseTimeoutMs);
      waiter.timer.unref();
      waiters.push(waiter);
    });
  }
  function send(value) {
    if (failure) throw failure;
    child.stdin.write(JSON.stringify(value) + '\n');
  }
  function untilExit(milliseconds) {
    if (closed) return Promise.resolve(closed);
    return new Promise(resolve => {
      const timer = setTimeout(() => resolve(null), milliseconds);
      timer.unref();
      exit.then(value => { clearTimeout(timer); resolve(value); });
    });
  }
  async function stop() {
    // Only this helper's child is stopped; no process-wide or PID-based cleanup.
    if (!closed && !child.stdin.destroyed) child.stdin.end(JSON.stringify({ command: 'stop' }) + '\n');
    let outcome = await untilExit(5000);
    const timedOut = !outcome;
    if (!outcome) { child.kill('SIGTERM'); outcome = await untilExit(1000); }
    if (!outcome) { child.kill('SIGKILL'); outcome = await untilExit(1000); }
    reader.close();
    fail('Fixture has stopped');
    return { ...(outcome || { code: null, signal: null }), timed_out: timedOut,
      cleanup_failed: !outcome, stderr_present: stderrPresent };
  }
  return { next, send, stop };
}

module.exports = { startFixture };
