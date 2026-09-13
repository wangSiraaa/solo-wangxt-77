import { Component, input } from '@angular/core';
import { DatePipe } from '@angular/common';
import { GroupsResponse } from '../models';

/** 样本分组看板：来源组、跨月标记、孤儿派生样本、身份冲突 */
@Component({
  selector: 'app-group-board',
  imports: [DatePipe],
  template: `
    <h2>样本分组（来源身份）</h2>

    @if (data()!.conflicts.length) {
      <div class="banner conflict">
        ⚠ {{ data()!.conflicts.length }} 个身份冲突：声明来源与派生链继承身份不一致，需先解决
        @for (c of data()!.conflicts; track c.child_sample_id) {
          <div class="conflict-row">
            样本 #{{ c.child_sample_id }} 声明来源 #{{ c.declared_source_id }}，
            但沿派生链继承自样本 #{{ c.parent_sample_id }} 的来源 #{{ c.inherited_source_id }}
            — 已保守合并为一组（不会拆到两侧），请人工裁决
          </div>
        }
      </div>
    }
    @if (data()!.orphans.length) {
      <div class="banner warn">
        ⚠ {{ data()!.orphans.length }} 个派生样本缺失来源关系（裁切/增强/转码未登记），
        各自成为独立组，可能造成未知泄漏
      </div>
    }

    <div class="groups">
      @for (g of data()!.groups; track g.group_id) {
        <div class="group-card" [class.straddle]="g.span_months.length > 1">
          <header>
            <strong>组 {{ g.group_id }}</strong>
            <span>{{ g.source_keys.join(', ') || '（无来源）' }}</span>
            <span class="badge">{{ g.size }} 样本</span>
            @if (g.span_months.length > 1) {
              <span class="badge warn" title="主体跨月份，时间边界无法无损切分">
                跨月: {{ g.span_months.join(' → ') }}</span>
            }
          </header>
          <table>
            @for (m of g.members; track m.sample_key) {
              <tr [class.orphan]="m.is_orphan">
                <td>{{ m.sample_key }}</td>
                <td>{{ m.label }}</td>
                <td><span class="kind">{{ m.kind }}</span></td>
                <td>{{ m.captured_at | date:'yyyy-MM-dd' }}</td>
                <td>@if (m.is_orphan) { <span class="badge danger">来源未知</span> }</td>
              </tr>
            }
          </table>
        </div>
      }
    </div>
  `,
  styles: [`
    .banner { padding: 8px 12px; border-radius: 6px; margin: 8px 0; }
    .banner.conflict { background: #fdecea; color: #b71c1c; }
    .conflict-row { font-size: 12px; margin-top: 4px; color: #7f0000; }
    .banner.warn { background: #fff8e1; color: #8d6e00; }
    .groups { display: flex; flex-wrap: wrap; gap: 12px; }
    .group-card { border: 1px solid #ddd; border-radius: 8px; padding: 10px; min-width: 320px; }
    .group-card.straddle { border-color: #ef9a9a; }
    header { display: flex; gap: 8px; align-items: center; margin-bottom: 6px; }
    .badge { background: #eceff1; border-radius: 10px; padding: 1px 8px; font-size: 12px; }
    .badge.warn { background: #ffe0b2; }
    .badge.danger { background: #ffcdd2; color: #b71c1c; }
    table { width: 100%; font-size: 12px; border-collapse: collapse; }
    td { padding: 2px 4px; border-top: 1px solid #f0f0f0; }
    tr.orphan { background: #fff3e0; }
    .kind { color: #666; }
  `]
})
export class GroupBoard {
  data = input.required<GroupsResponse>();
}
