import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable } from 'rxjs';
import { GroupsResponse, SplitDetail, SplitReport, VerifyResult } from './models';

@Injectable({ providedIn: 'root' })
export class Api {
  private http = inject(HttpClient);
  private base = 'http://localhost:8000';

  createDataset(name: string): Observable<{ id: number }> {
    return this.http.post<{ id: number }>(`${this.base}/datasets`, null, { params: { name } });
  }

  getGroups(datasetId: number): Observable<GroupsResponse> {
    return this.http.get<GroupsResponse>(`${this.base}/datasets/${datasetId}/groups`);
  }

  generateSplit(datasetId: number, targetEvalRatio: number, timeBoundary: string, seed: number) {
    return this.http.post<{ split_id: number; version_no: number; report: SplitReport }>(
      `${this.base}/datasets/${datasetId}/splits:generate`,
      { target_eval_ratio: targetEvalRatio, time_boundary: timeBoundary, seed });
  }

  getSplit(splitId: number): Observable<SplitDetail> {
    return this.http.get<SplitDetail>(`${this.base}/splits/${splitId}`);
  }

  verify(splitId: number): Observable<VerifyResult> {
    return this.http.post<VerifyResult>(`${this.base}/splits/${splitId}/verify`, null);
  }

  confirm(splitId: number, confirmedBy: string) {
    return this.http.post<{ split_id: number; status: string; manifest_hash: string }>(
      `${this.base}/splits/${splitId}/confirm`, { confirmed_by: confirmedBy });
  }

  revise(splitId: number, targetEvalRatio: number, timeBoundary: string, seed: number) {
    return this.http.post<any>(`${this.base}/splits/${splitId}/revise`,
      { target_eval_ratio: targetEvalRatio, time_boundary: timeBoundary, seed });
  }
}
