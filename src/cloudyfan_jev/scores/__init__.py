"""Read-only benchmark ingestion, native-scale pools, immutable run bindings."""
from .catalog import compile_snapshot, select_profiles, verify_snapshot
from .integration import ScoreCore
from .snapshots import SnapshotStore
from .qualification import build_qualification_pack, qualified_snapshot
from .updates import ScoreUpdater, preview_update

__all__ = ['compile_snapshot', 'select_profiles', 'verify_snapshot', 'ScoreCore', 'SnapshotStore',
           'build_qualification_pack', 'qualified_snapshot', 'ScoreUpdater', 'preview_update']

from .categories import compile_category_matrix, resolve_category, verify_category_matrix
from .category_routing import CategoryCore, prepare_category_selection

__all__ += ['compile_category_matrix', 'resolve_category', 'verify_category_matrix',
            'CategoryCore', 'prepare_category_selection']

from .backup_pools import prepare_backup_pools, verify_backup_pools, backup_coverage_report
from .health import HealthStore, HealthError
from .backup_runtime import (BackupCategoryCore, backup_pool_contract_hash,
                             backup_flow_contract_hash, backup_classifier_contract_hash)

__all__ += ['prepare_backup_pools', 'verify_backup_pools', 'backup_coverage_report',
            'HealthStore', 'HealthError', 'BackupCategoryCore', 'backup_pool_contract_hash',
            'backup_flow_contract_hash', 'backup_classifier_contract_hash']
