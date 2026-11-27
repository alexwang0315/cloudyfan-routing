"""What a client does with a message it received: describe it, and store any file it carries."""
from pathlib import Path

from .envelope import Kind
from .transfer import Manifest


def save_path(folder, name, message_id):
    target = Path(folder) / (name or message_id)
    if target.exists():
        target = target.with_name(f'{target.stem}-{message_id[:8]}{target.suffix}')
    return target


def store(client, envelope, save_dir):
    """Return a plain description of the message, downloading or writing any file into `save_dir`."""
    record = {'id': envelope.id, 'sender': envelope.sender, 'kind': envelope.kind.name.lower(),
              'size': envelope.size, 'created_at': envelope.created_at}
    if envelope.kind is Kind.TRANSFER:
        manifest = Manifest.from_envelope(envelope)
        Path(save_dir).mkdir(parents=True, exist_ok=True)
        target = save_path(save_dir, manifest.name, envelope.id)
        client.fetch(envelope, target)
        record.update(file=str(target), name=manifest.name, size=manifest.size, sha256=manifest.sha256)
    elif envelope.kind is Kind.FILE:
        Path(save_dir).mkdir(parents=True, exist_ok=True)
        target = save_path(save_dir, '', envelope.id)
        target.write_bytes(envelope.body)
        record.update(file=str(target), sha256=envelope.sha256)
    else:
        record['text'] = envelope.body.decode('utf-8', 'replace')
    return record
