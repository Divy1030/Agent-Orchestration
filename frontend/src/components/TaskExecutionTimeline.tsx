import { CheckCircle, CircleNotch, FileCode, GitBranch, TestTube } from '@phosphor-icons/react';

export default function TaskExecutionTimeline({ task }: { task: any }) {
  const execution = task.execution || {};
  const events = execution.events || [];
  const files = execution.files_changed || [];
  const tests = execution.tests || [];
  return <div className="task-execution-timeline"><div className="task-execution-meta"><span><GitBranch size={13}/> {execution.branch || 'Preparing isolated branch'}</span>{execution.current_file && <span><FileCode size={13}/> {execution.current_file}</span>}<span className={execution.status === 'running' ? 'live' : ''}>{execution.status === 'running' ? <CircleNotch className="task-output-spinner-small" size={13}/> : <CheckCircle size={13}/>} {execution.phase || execution.status || 'idle'}</span></div>{files.length > 0 && <div className="task-execution-files"><strong>Files changed</strong>{files.map((file: string) => <code key={file}>{file}</code>)}</div>}{tests.length > 0 && <div className="task-execution-tests"><strong><TestTube size={13}/> Tests</strong>{tests.map((test: any, index: number) => <span key={`${test.name || 'test'}-${index}`}>{test.name || 'Verification'} · {test.status}</span>)}</div>}{events.slice(-8).map((event: any, index: number) => <div className="task-execution-event" key={`${event.created_at || 'event'}-${index}`}><i/ ><span>{event.message}</span></div>)}</div>;
}
