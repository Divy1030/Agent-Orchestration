import { useEffect, useMemo, useState } from 'react';
import { GitBranch, GitCommit, GitPullRequest, ShieldCheck, X } from '@phosphor-icons/react';
import { api } from '../lib/api';
import SquareButton from './SquareButton';
import TaskOutputMarkdown from './TaskOutputMarkdown';

type Props = {
  spaceId: string;
  tasks: any[];
  agents: any[];
  baseBranch: string;
  canCommit: boolean;
  onChanged: () => Promise<void>;
};

type DiffTarget = { task: any; file: any };

function contentLines(value: string) {
  return value ? value.split('\n') : [];
}

function diffCounts(before: string, after: string) {
  const oldLines = contentLines(before);
  const newLines = contentLines(after);
  const oldSet = new Set(oldLines);
  const newSet = new Set(newLines);
  return {
    added: newLines.filter(line => !oldSet.has(line)).length,
    removed: oldLines.filter(line => !newSet.has(line)).length,
  };
}

export default function SpaceProjectReview({ spaceId, tasks, agents, baseBranch, canCommit, onChanged }: Props) {
  const [baseContents, setBaseContents] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState('');
  const [message, setMessage] = useState('');
  const [activeDiff, setActiveDiff] = useState<DiffTarget | null>(null);
  const reviewTasks = useMemo(
    () => tasks.filter(task => task.execution?.review_status !== 'closed' && (task.execution?.proposed_changes?.files?.length || task.status === 'review' || (task.status === 'done' && (task.execution?.result?.role === 'coder' || task.execution?.result?.role === 'fix' || task.task_type === 'code' || task.task_type === 'fix')))),
    [tasks],
  );

  useEffect(() => {
    let active = true;
    const files = reviewTasks
      .flatMap(task => task.execution?.proposed_changes?.files || [])
      .map((file: any) => file.path)
      .filter((path: string, index: number, all: string[]) => path && all.indexOf(path) === index);

    if (!files.length) {
      setBaseContents({});
      return () => { active = false; };
    }

    void Promise.all(files.map(async path => {
      try {
        const result = await api<{ file: { content: string } }>(
          `/api/spaces/${spaceId}/github/file?path=${encodeURIComponent(path)}&branch=${encodeURIComponent(baseBranch)}`,
        );
        return [path, result.file?.content || ''] as const;
      } catch (error: any) {
        if (active) setMessage(`Could not load ${path} from ${baseBranch}: ${error.message || 'GitHub request failed'}`);
        return [path, ''] as const;
      }
    })).then(rows => {
      if (active) setBaseContents(Object.fromEntries(rows));
    });

    return () => { active = false; };
  }, [spaceId, baseBranch, reviewTasks]);

  useEffect(() => {
    if (!activeDiff) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setActiveDiff(null);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [activeDiff]);

  const action = async (task: any, name: string, request: () => Promise<any>) => {
    setBusy(`${name}:${task.id}`);
    setMessage('');
    try {
      await request();
      await onChanged();
      setMessage(name === 'commit' ? 'Changes committed to the task branch.' : name === 'pr' ? 'Pull request created.' : 'Review decision saved.');
    } catch (error: any) {
      setMessage(error.message);
    } finally {
      setBusy('');
    }
  };

  const closeReview = (task: any) => {
    const outcome = window.prompt('Review outcome: accepted, rejected, superseded, no_changes, or resolved', 'resolved');
    if (!outcome) return;
    const allowed = ['accepted', 'rejected', 'superseded', 'no_changes', 'resolved'];
    if (!allowed.includes(outcome)) { setMessage('Choose a valid review outcome.'); return; }
    void action(task, 'close-review', () => api(`/api/spaces/${spaceId}/tasks/${task.id}/close-review`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ outcome }) }));
  };

  const renderActions = (task: any, files: any[], execution: any, role: string) => (
    <div className="project-review-actions">
      {role === 'planner' || role === 'thinker' ? (
        <>
          <SquareButton disabled={!!busy} onClick={() => void action(task, 'accept-plan', () => api(`/api/spaces/${spaceId}/tasks/${task.id}/advance`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode: 'auto' }) }))}>
            Accept plan
          </SquareButton>
          <SquareButton tone="danger" disabled={!!busy} onClick={() => void action(task, 'reject-plan', () => api(`/api/spaces/${spaceId}/tasks/${task.id}/reject-proposal`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ reason: 'Plan needs revision.' }) }))}>
            Reject plan
          </SquareButton>
          <SquareButton tone="quiet" disabled={!!busy} onClick={() => void action(task, 'retry-plan', () => api(`/api/spaces/${spaceId}/tasks/${task.id}/retry`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ reason: 'Please revise the plan.' }) }))}>
            Retry plan
          </SquareButton>
        </>
      ) : role === 'tester' ? (
        <SquareButton tone="quiet" disabled={!!busy} onClick={() => void action(task, 'retry-test', () => api(`/api/spaces/${spaceId}/tasks/${task.id}/retry`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ reason: 'Run verification again.' }) }))}>
          Retry test
        </SquareButton>
      ) : (role === 'coder' || role === 'fix') && !files.length ? (
        <SquareButton tone="quiet" disabled={!!busy} onClick={() => void action(task, 'retry-coder', () => api(`/api/spaces/${spaceId}/tasks/${task.id}/retry`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ reason: 'The coder did not return a complete structured file proposal.' }) }))}>
          Retry coder
        </SquareButton>
      ) : canCommit ? (
        <SquareButton disabled={!!busy || !files.length || !execution.branch} onClick={() => void action(task, 'commit', () => api(`/api/spaces/${spaceId}/tasks/${task.id}/apply-proposal`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ paths: files.map((file: any) => file.path), message: `Apply reviewed changes: ${task.title}` }) }))}>
          <GitCommit size={15} /> Approve &amp; commit
        </SquareButton>
      ) : (
        <SquareButton tone="quiet" disabled={!!busy || !execution.branch} onClick={() => void action(task, 'pr', () => api(`/api/spaces/${spaceId}/tasks/${task.id}/pull-request`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: task.title, body: task.description || 'Agent changes submitted for review.' }) }))}>
          <GitPullRequest size={15} /> Create pull request
        </SquareButton>
      )}
      <SquareButton tone="danger" disabled={!!busy} onClick={() => void action(task, 'reject', () => api(`/api/spaces/${spaceId}/tasks/${task.id}/reject-proposal`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ reason: 'Changes requested from Review.' }) }))}>
        Request changes
      </SquareButton>
      {canCommit && <SquareButton tone="quiet" disabled={!!busy} onClick={() => closeReview(task)}>Close review</SquareButton>}
    </div>
  );

  return (
    <section className="project-review-page">
      <div className="project-review-heading">
        <div><span className="eyebrow">CHANGE REVIEW</span><h2>Review changes</h2><p>Inspect agent work before it is committed or submitted as a pull request.</p></div>
        <ShieldCheck size={24} />
      </div>
      {message && <p className="settings-runtime-note">{message}</p>}
      {!reviewTasks.length && <div className="project-review-empty"><ShieldCheck size={22} /><div><strong>No pending changes</strong><p>Completed agent proposals and tasks ready for review will appear here.</p></div></div>}
      {reviewTasks.map(task => {
        const execution = task.execution || {};
        const agent = agents.find(item => item.id === task.assignee_agent_id);
        const tests = execution.tests || execution.result?.tests || [];
        const role = execution.result?.role || agent?.role || task.task_type;
        const proposal = execution.proposed_changes || {};
        const files = role === 'coder' || role === 'fix' ? (proposal.files || []) : [];
        const planOutput = execution.result?.reply || '';
        return (
          <article className="project-review-card" key={task.id}>
            <header className="project-review-card-heading"><div><h3>{task.title}</h3><p>{task.task_type || 'code'} · {task.status.replace('_', ' ')} · {proposal.status || 'pending review'}</p></div><span className={`review-status review-status-${task.status}`}>{task.status.replace('_', ' ')}</span></header>
            <div className="project-review-meta"><span><strong>Agent</strong>{agent?.name || 'Unassigned'}{agent?.role ? ` · ${agent.role}` : ''}</span><span><GitBranch size={14} /><strong>Branch</strong>{execution.branch || 'No task branch recorded'}</span><span><strong>Base</strong>{baseBranch}</span><span><strong>Tests</strong>{tests.length ? `${tests.length} result${tests.length === 1 ? '' : 's'}` : execution.test_status || 'Not run'}</span></div>
            <section className="project-review-criteria"><strong>Acceptance criteria</strong><p>{task.description || 'No acceptance criteria were provided.'}</p></section>
            {(role === 'planner' || role === 'thinker') && planOutput && <section className="project-review-plan"><div className="project-review-plan-heading"><strong>Planner output</strong><span>Plan only · no code changes</span></div><TaskOutputMarkdown content={planOutput} /></section>}
            {(role === 'coder' || role === 'fix') && !files.length && planOutput && <section className="project-review-plan project-review-coder-output"><div className="project-review-plan-heading"><strong>Coder output</strong><span>No structured file proposal was saved</span></div><TaskOutputMarkdown content={planOutput} /><p className="project-review-warning">This task cannot be committed or submitted as a pull request until the coder returns complete file changes.</p></section>}
            {files.length > 0 && <section className="project-review-diff"><div className="project-review-diff-heading"><strong>Side-by-side diff</strong><span>{files.length} file{files.length === 1 ? '' : 's'} · click to expand</span></div>{files.map((file: any) => { const before = file.before || file.previous_content || baseContents[file.path] || ''; return <div className="project-review-file" key={file.path}><button type="button" className="project-review-file-name review-diff-file-button" onClick={() => setActiveDiff({ task, file })}><GitCommit size={14} />{file.path}</button><div className="project-review-columns"><pre className="project-review-before">{before || 'No previous content supplied'}</pre><pre className="project-review-after">{file.content || file.after || ''}</pre></div></div>; })}</section>}
            {tests.length > 0 && <section className="project-review-tests"><strong>Test results</strong>{(Array.isArray(tests) ? tests : [tests]).map((test: any, index: number) => <div key={index} className={test.passed === false || test.status === 'failed' ? 'test-failed' : 'test-passed'}>{typeof test === 'string' ? test : `${test.name || test.command || 'Test'} · ${test.status || (test.passed === false ? 'failed' : 'passed')}`}</div>)}</section>}
            {renderActions(task, files, execution, role)}
          </article>
        );
      })}
      {activeDiff && (() => {
        const before = activeDiff.file.before || activeDiff.file.previous_content || baseContents[activeDiff.file.path] || '';
        const after = activeDiff.file.content || activeDiff.file.after || '';
        const counts = diffCounts(before, after);
        return <div className="review-diff-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) setActiveDiff(null); }}><section className="review-diff-dialog" role="dialog" aria-modal="true" aria-label={`Diff for ${activeDiff.file.path}`}><header className="review-diff-dialog-header"><div><span className="eyebrow">FILE CHANGE</span><strong>{activeDiff.file.path}</strong><small>{activeDiff.task.title} · base {baseBranch}</small></div><div className="review-diff-dialog-header-actions"><span className="review-diff-count added">+{counts.added}</span><span className="review-diff-count removed">-{counts.removed}</span><button type="button" aria-label="Close diff" onClick={() => setActiveDiff(null)}><X size={18} /></button></div></header><div className="review-diff-dialog-labels"><span>Base branch · {baseBranch}</span><span>Agent proposal</span></div><div className="review-diff-dialog-body"><pre className="review-diff-dialog-before">{before || 'No previous content supplied'}</pre><pre className="review-diff-dialog-after">{after || 'No proposed content supplied'}</pre></div>{renderActions(activeDiff.task, activeDiff.task.execution?.proposed_changes?.files || [], activeDiff.task.execution || 'coder')}</section></div>;
      })()}
    </section>
  );
}
