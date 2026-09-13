import { Component, inject, input, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { DecimalPipe, PercentPipe } from '@angular/common';
import { Api } from '../api';
import { SplitDetail, VerifyResult } from '../models';

/** 拆分方案面板：生成 → 报告 → 独立验证 → 确认锁定 → 修订新版本 */
@Component({
  selector: 'app-split-panel',
  imports: [FormsModule, DecimalPipe, PercentPipe],
  template: `
    <h2>拆分方案</h2>
    <div class="form">
      <label>评测目标比例 <input type="number" step="0.05" min="0.05" max="0.9"
        [(ngModel)]="targetRatio"></label>
      <label>时间边界 <input type="datetime-local" [(ngModel)]="boundary"></label>
      <label>随机种子 <input type="number" [(ngModel)]="seed"></label>
      @if (!split() || split()!.status !== 'locked') {
        <button (click)="generate()">生成方案</button>
      }
      @if (split()?.status === 'locked') {
        <button (click)="revise()">修改（形成新版本）</button>
      }
    </div>

    @if (split(); as s) {
      <div class="status">
        版本 v{{ s.version_no }} · 状态
        <strong [class]="s.status">{{ statusLabel(s.status) }}</strong>
        @if (s.manifest_hash) { · 清单哈希 <code>{{ s.manifest_hash }}</code> }
      </div>

      <h3>类别比例（目标 vs 实际）</h3>
      <table class="ratio">
        <tr><th>类别</th><th>总数</th><th>评测数</th><th>实际占比</th><th>目标</th></tr>
        @for (row of classRows(s); track row.label) {
          <tr [class.off]="row.off">
            <td>{{ row.label }}</td><td>{{ row.total }}</td><td>{{ row.eval }}</td>
            <td>{{ row.share | percent:'1.0-1' }}</td>
            <td>{{ s.report.target_eval_ratio | percent:'1.0-1' }}</td>
          </tr>
        }
      </table>
      <div class="cost">
        时间违例 <strong>{{ s.report.cost.time_violations }}</strong> 条 ·
        平均比例偏差 <strong>{{ s.report.cost.mean_abs_ratio_deviation | number:'1.2-2' }}</strong> ·
        跨边界组 {{ s.report.n_straddling_groups }} 个
      </div>

      <h3>代价解释</h3>
      <ul class="expl">
        @for (e of s.report.explanations; track e) { <li>{{ e }}</li> }
        @if (!s.report.explanations.length) { <li>所有目标均达成，无代价。</li> }
      </ul>

      <h3>独立验证 <button (click)="runVerify()">重新验证</button></h3>
      @if (verify(); as v) {
        <div class="banner" [class.ok]="v.passed" [class.bad]="!v.passed">
          分组隔离：{{ v.group_isolation.ok ? '通过' : '失败' }}
          （{{ v.group_isolation.n_groups_checked }} 组受检）·
          时间违例 {{ v.time_condition.n_violations }} 条
        </div>
        <div class="banner warn">
          未知关系残留风险：{{ v.unknown_relation_risks.summary }}
        </div>
        @for (o of v.unknown_relation_risks.orphan_derived_samples; track o.sample_id) {
          <div class="risk">孤儿派生样本 #{{ o.sample_id }}（{{ o.kind }}，{{ o.side }} 侧）— {{ o.risk }}</div>
        }
        @for (d of v.unknown_relation_risks.cross_side_duplicate_content; track d.content_hash) {
          <div class="risk">重复内容横跨两侧：样本 {{ d.sample_ids.join(', ') }} — {{ d.risk }}</div>
        }
      }

      @if (s.status === 'draft') {
        <button class="primary" (click)="confirm()">确认并锁定清单哈希</button>
      }
    }
  `,
  styles: [`
    .form { display: flex; gap: 12px; align-items: end; flex-wrap: wrap; margin-bottom: 12px; }
    label { display: flex; flex-direction: column; font-size: 12px; color: #555; }
    button { padding: 6px 14px; border-radius: 6px; border: 1px solid #90a4ae;
             background: #fff; cursor: pointer; }
    button.primary { background: #1565c0; color: #fff; border: none; padding: 8px 18px; }
    .status { margin: 8px 0; }
    .status .locked { color: #2e7d32; } .status .draft { color: #ef6c00; }
    .status .superseded { color: #757575; }
    code { font-size: 11px; background: #f5f5f5; padding: 2px 4px; border-radius: 4px; }
    table.ratio { border-collapse: collapse; }
    table.ratio td, table.ratio th { border: 1px solid #ddd; padding: 4px 10px; }
    tr.off td { background: #fff8e1; }
    .cost { margin: 6px 0; color: #555; }
    .expl li { margin: 4px 0; font-size: 13px; }
    .banner { padding: 8px 12px; border-radius: 6px; margin: 6px 0; }
    .banner.ok { background: #e8f5e9; color: #1b5e20; }
    .banner.bad { background: #fdecea; color: #b71c1c; }
    .banner.warn { background: #fff8e1; color: #8d6e00; }
    .risk { font-size: 12px; color: #6d4c41; margin: 2px 0 2px 12px; }
  `]
})
export class SplitPanel {
  datasetId = input.required<number>();
  private api = inject(Api);

  targetRatio = 0.25;
  boundary = '2026-02-01T00:00';
  seed = 42;

  split = signal<SplitDetail | null>(null);
  verify = signal<VerifyResult | null>(null);

  classRows(s: SplitDetail) {
    return Object.entries(s.report.per_class).map(([label, r]) => ({
      label, total: r.total, eval: r.eval, share: r.achieved_eval_share,
      off: Math.abs(r.achieved_eval_share - s.report.target_eval_ratio) > 0.05,
    }));
  }

  statusLabel(status: string) {
    return { draft: '草稿', locked: '已锁定', superseded: '已被新版本取代' }[status] ?? status;
  }

  private refresh(splitId: number) {
    this.api.getSplit(splitId).subscribe(s => { this.split.set(s); this.runVerify(); });
  }

  generate() {
    this.api.generateSplit(this.datasetId(), this.targetRatio,
      new Date(this.boundary).toISOString(), this.seed)
      .subscribe(r => this.refresh(r.split_id));
  }

  runVerify() {
    const s = this.split();
    if (s) this.api.verify(s.split_id).subscribe(v => this.verify.set(v));
  }

  confirm() {
    const s = this.split();
    if (s) this.api.confirm(s.split_id, 'ml-lead').subscribe(() => this.refresh(s.split_id));
  }

  revise() {
    const s = this.split();
    if (s) this.api.revise(s.split_id, this.targetRatio,
      new Date(this.boundary).toISOString(), this.seed + 1)
      .subscribe(r => this.refresh(r.new.split_id));
  }
}
