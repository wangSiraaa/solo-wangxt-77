import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Api } from './api';
import { GroupsResponse } from './models';
import { GroupBoard } from './group-board/group-board';
import { SplitPanel } from './split-panel/split-panel';

@Component({
  selector: 'app-root',
  imports: [FormsModule, GroupBoard, SplitPanel],
  templateUrl: './app.html',
  styleUrl: './app.css'
})
export class App {
  private api = inject(Api);

  datasetId = signal<number | null>(null);
  groups = signal<GroupsResponse | null>(null);
  datasetName = 'demo-face-v1';
  datasetIdInput = 1;

  createDataset() {
    this.api.createDataset(this.datasetName)
      .subscribe(d => { this.datasetId.set(d.id); this.loadGroups(); });
  }

  useExisting() {
    this.datasetId.set(this.datasetIdInput);
    this.loadGroups();
  }

  loadGroups() {
    const id = this.datasetId();
    if (id != null) this.api.getGroups(id).subscribe(g => this.groups.set(g));
  }
}
