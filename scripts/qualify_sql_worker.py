"""Inspect a prospective Linux SQL target; explicit exercise uses mocked models.

No provisioning, downloads, host-policy changes, real API or formal approval.
Normal SQL exercise is only part of the required adversarial target evidence.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import shutil
import sqlite3
import sys

from cloudyfan_jev.sql_isolation import LinuxIsolation


REQUIRED_EXERCISES = ('total_memory_and_descendants', 'cpu_limit', 'external_deadline',
                      'host_file_read_write_denial', 'network_namespace_denial',
                      'cancellation_and_reaping', 'interrupted_answer_and_budget_resume')


def mock_normal_flow(policy, output):
    """Real SQL worker and core with strictly MockTransport model I/O."""
    import httpx
    from cloudyfan_jev.budget import Authorization, BudgetStore, BudgetedCore, EnforcedBound
    from cloudyfan_jev.client import Client
    from cloudyfan_jev.demo import example_config
    from cloudyfan_jev.engine import Core
    from cloudyfan_jev.sql import SQL_DIAGNOSTICS, run_sql
    from cloudyfan_jev.sql_catalog import contract
    from cloudyfan_jev.sql_review import cases
    calls = []
    rows = []
    for case in cases():
        def respond(request):
            payload = json.loads(request.content)
            if not request.url.path.endswith('/chat/completions'):
                raise ValueError('Qualification mock must not dispatch classification')
            calls.append(case.id)
            return httpx.Response(200,json={'id':f'gen-qualification-{case.id}','model':payload['model'],
                'provider':'Example','usage':{'cost':0.01},'choices':[{'finish_reason':'stop',
                'message':{'content':json.dumps({'sql':case.reference_sql})}}]})
        profiles,decision = example_config()
        with httpx.Client(transport=httpx.MockTransport(respond)) as http:
            engine = Core(Client(http,'mock-only-qualification',output/case.id),profiles,decision,
                          diagnostic_contract=SQL_DIAGNOSTICS)
            store = BudgetStore(output/(case.id+'-budget.sqlite'),
                                clock=lambda:datetime(2026,10,18,tzinfo=timezone.utc))
            store.authorize(Authorization('synthetic-only','1','2026-10-19T00:00:00+00:00'))
            bounds = {p['model_version']:EnforcedBound('.03','synthetic') for p in profiles.values()}
            bounds[decision['version']] = EnforcedBound('.001','synthetic')
            controlled = BudgetedCore(engine,store,model_bounds=bounds,tool_bounds={},verify_bound=lambda *a:True)
            task = contract(case.id,isolation=policy)
            first = run_sql(controlled,'qualification',task)
            second = run_sql(controlled,'qualification',task)
            rows.append({'id':case.id,'passed':first['status']=='passed','completed_reuse':first==second,
                         'synthetic_budget':store.report()})
            print(case.id+': normal flow checked', file=sys.stderr, flush=True)
    return {'cases':rows,'mock_posts':len(calls),'passed':all(r['passed'] and r['completed_reuse']
             and r['synthetic_budget']['reserved_usd']=='0' for r in rows) and len(calls)==8}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy',type=Path,help='Reviewed explicit LinuxIsolation JSON configuration')
    parser.add_argument('--exercise',action='store_true')
    parser.add_argument('--adversarial', action='store_true',
                        help='Explicit controlled resource/isolation evidence, never formal approval')
    parser.add_argument('--authorization-ref')
    parser.add_argument('--output',type=Path,help='New private evidence directory for explicit exercise')
    args = parser.parse_args(argv)
    if args.adversarial and not args.exercise:
        parser.error('--adversarial requires --exercise and its target/authorization/output arguments')
    if args.exercise and (not args.policy or not args.authorization_ref or not args.output):
        parser.error('--exercise requires --policy, --authorization-ref and --output')
    report = {'schema_version':1,'kind':'target-qualification-candidate','result':'blocked',
              'recorded_at':datetime.now(timezone.utc).isoformat(),'formal_qualification':'pending',
              'platform':{'system':sys.platform,'kernel':platform.release(),'machine':platform.machine(),
                          'python':platform.python_version(),'sqlite':sqlite3.sqlite_version},
              'available_local_tools':{n:shutil.which(n) is not None for n in ('bwrap','docker','podman','limactl')},
              'real_api_calls':0,'processes_launched':0,
              'required_target_exercises':[{'id':name,'result':'not_run'} for name in REQUIRED_EXERCISES]}
    output_admitted = False
    try:
        policy = LinuxIsolation(**json.loads(args.policy.read_text())) if args.policy else None
        if policy:
            report['policy'] = policy.descriptor()
            report['policy_file_sha256'] = hashlib.sha256(args.policy.read_bytes()).hexdigest()
        report['blocker'] = 'authorized Linux target and pinned artifacts are required'
        if sys.platform == 'linux' and policy:
            if not args.exercise:
                report['result'] = 'inspection_only'
            else:
                # Fail-closed preflight happens in SqlEnvironment before mock POST.
                # Never overwrite previous evidence or source directories.
                args.output.mkdir(parents=True,exist_ok=False)
                output_admitted = True
                report['authorization_ref'] = args.authorization_ref
                report['processes_launched'] = None
                report['process_count_status'] = 'exercise started; individual launches not counted'
                report['normal_flow'] = mock_normal_flow(policy,args.output)
                report['result'] = 'partial' if report['normal_flow']['passed'] else 'failed'
                report['blocker'] = 'adversarial target exercises and named review remain required'
                if args.adversarial and report['normal_flow']['passed']:
                    from sql_target_exercises import run_target_exercises
                    report['required_target_exercises'] = run_target_exercises(policy, args.output)
                    complete = (tuple(r['id'] for r in report['required_target_exercises']) == REQUIRED_EXERCISES
                                and all(r['result'] == 'passed' for r in report['required_target_exercises']))
                    report['result'] = 'evidence_complete' if complete else 'partial'
                    report['blocker'] = ('named formal review remains required' if complete
                                         else 'target evidence is incomplete; retain every failed/not-run row')
    except (ValueError,TypeError,OSError,RuntimeError) as error:
        report['result'] = 'blocked'
        report['blocker'] = getattr(error,'code','TARGET_CONFIGURATION_OR_EXERCISE_UNAVAILABLE')
    if output_admitted:
        # Append-only output folder admission occurred before any worker/model I/O.
        with (args.output/'report.json').open('x') as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 2  # Neither inspection nor partial exercise is formal qualification.


if __name__ == '__main__':
    raise SystemExit(main())
