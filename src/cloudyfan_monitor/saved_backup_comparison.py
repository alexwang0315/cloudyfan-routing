"""Host-side original Backup evidence loader; never a dispatch capability."""


def reproduce_saved_policy(source, binding, job, manifest, result, run, trusted, *, original_path, receipts):
    from .saved_comparisons import _reproduce_backup_initial
    return _reproduce_backup_initial(source, binding, job, manifest, result, run, trusted,
                                     original_path=original_path, receipts=receipts)
