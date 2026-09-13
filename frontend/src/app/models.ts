export interface GroupMember {
  sample_key: string; label: string; kind: string;
  captured_at: string; is_orphan: boolean;
}

export interface GroupView {
  group_id: number; source_keys: string[]; size: number;
  labels: string[]; span_months: string[]; members: GroupMember[];
}

export interface IdentityConflict {
  parent_sample_id: number; child_sample_id: number;
  declared_source_id: number; inherited_source_id: number; reason: string;
}

export interface GroupsResponse {
  groups: GroupView[]; conflicts: IdentityConflict[]; orphans: number[];
}

export interface SplitReport {
  seed: number; target_eval_ratio: number; time_boundary: string;
  n_groups: number; n_straddling_groups: number;
  cost: { time_violations: number; mean_abs_ratio_deviation: number;
          per_class_eval_share: Record<string, number> };
  explanations: string[];
  per_class: Record<string, { total: number; eval: number; achieved_eval_share: number }>;
  identity_conflicts: IdentityConflict[];
  orphan_sample_ids: number[];
}

export interface AssignmentRow {
  sample_key: string; side: 'train' | 'eval'; label: string;
  kind: string; source_id: number | null; captured_at: string;
}

export interface SplitDetail {
  split_id: number; version_no: number; status: string; seed: number;
  params: any; manifest_hash: string | null; report: SplitReport;
  assignments: AssignmentRow[];
}

export interface VerifyResult {
  passed: boolean;
  group_isolation: { ok: boolean; n_groups_checked: number; violations: any[] };
  time_condition: { ok: boolean; boundary: string; n_violations: number; violations: any[] };
  class_ratio: Record<string, { achieved_eval_share: number; target: number; gap: number }>;
  unknown_relation_risks: {
    orphan_derived_samples: any[];
    cross_side_duplicate_content: any[];
    summary: string;
  };
}
