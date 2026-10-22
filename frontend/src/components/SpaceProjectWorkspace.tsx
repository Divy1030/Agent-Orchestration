import { useEffect, useState } from 'react';
import { GitBranch, GitCommit, GitPullRequest, FileCode } from '@phosphor-icons/react';
import { api } from '../lib/api';
import SquareButton from './SquareButton';

type Props = { spaceId: string; defaultBranch: string; canManage: boolean };

export default function SpaceProjectWorkspace({ spaceId, defaultBranch, canManage }: Props) {
  const [branch, setBranch] = useState(defaultBranch || 'main');
  const [snapshot, setSnapshot] = useState<any>(null), [selectedPath, setSelectedPath] = useState(''), [content, setContent] = useState('');
  const [newBranch, setNewBranch] = useState(''), [commitMessage, setCommitMessage] = useState(''), [prTitle, setPrTitle] = useState(''), [prBody, setPrBody] = useState('');
  const [message, setMessage] = useState(''), [busy, setBusy] = useState(false);

  const loadSnapshot = async (selectedBranch = branch) => {
    const result = await api<{ snapshot: any }>(`/api/spaces/${spaceId}/github/snapshot?branch=${encodeURIComponent(selectedBranch)}`);
    setSnapshot(result.snapshot);
    setBranch(result.snapshot.branch);
  };
  useEffect(() => { void loadSnapshot().catch((error: any) => setMessage(error.message)); }, [spaceId]);

  const openFile = async (path: string) => {
    setBusy(true);
    try { const result = await api<{ file: { content: string } }>(`/api/spaces/${spaceId}/github/file?path=${encodeURIComponent(path)}&branch=${encodeURIComponent(branch)}`); setSelectedPath(path); setContent(result.file.content); } catch (error: any) { setMessage(error.message); } finally { setBusy(false); }
  };
  const createNewBranch = async () => {
    setBusy(true);
    try { await api(`/api/spaces/${spaceId}/github/branches`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ branch: newBranch, from_branch: branch }) }); setBranch(newBranch); setNewBranch(''); setMessage(`Branch ${newBranch} created.`); await loadSnapshot(newBranch); } catch (error: any) { setMessage(error.message); } finally { setBusy(false); }
  };
  const commit = async () => {
    setBusy(true);
    try { const result = await api<{ commit: { commit_sha: string } }>(`/api/spaces/${spaceId}/github/commits`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ branch, message: commitMessage, files: [{ path: selectedPath, content }] }) }); setCommitMessage(''); setMessage(`Committed ${result.commit.commit_sha.slice(0, 7)} on ${branch}.`); await loadSnapshot(); } catch (error: any) { setMessage(error.message); } finally { setBusy(false); }
  };
  const createPr = async () => {
    setBusy(true);
    try { const result = await api<{ pull_request: { url: string } }>(`/api/spaces/${spaceId}/github/pull-requests`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: prTitle, body: prBody, head: branch, base: snapshot?.default_branch || defaultBranch }) }); setPrTitle(''); setPrBody(''); setMessage(`Pull request created: ${result.pull_request.url}`); } catch (error: any) { setMessage(error.message); } finally { setBusy(false); }
  };

  if (!snapshot) return <section className="space-project-workspace"><p className="inspector-empty">Loading repository…</p>{message && <p className="settings-runtime-note">{message}</p>}</section>;
  const files = snapshot.tree.filter((item: any) => item.type === 'blob');
  return <section className="space-project-workspace"><div className="project-workspace-toolbar"><div><span className="eyebrow">REPOSITORY WORKSPACE</span><h2>{snapshot.full_name}</h2><p>Review files, work on an isolated branch, then open a pull request.</p></div><label className="branch-picker"><GitBranch size={15}/><select value={branch} onChange={event => { setBranch(event.target.value); void loadSnapshot(event.target.value).catch((error: any) => setMessage(error.message)); }}>{[snapshot.default_branch, branch].filter((item, index, all) => item && all.indexOf(item) === index).map(item => <option key={item}>{item}</option>)}</select></label></div><div className="project-workspace-grid"><aside className="project-file-tree"><strong>Files</strong>{files.map((file: any) => <button className={selectedPath === file.path ? 'selected' : ''} key={file.path} onClick={() => void openFile(file.path)}><FileCode size={14}/>{file.path}</button>)}{!files.length && <p>No text files found.</p>}</aside><div className="project-editor"><div className="project-editor-heading"><span>{selectedPath || 'Select a file'}</span>{selectedPath && <span>{content.length.toLocaleString()} chars</span>}</div><textarea value={content} onChange={event => setContent(event.target.value)} disabled={!selectedPath || !canManage} placeholder="Select a file to inspect it…" spellCheck={false}/><div className="project-editor-actions">{canManage && <><input value={commitMessage} onChange={event => setCommitMessage(event.target.value)} placeholder="Commit message"/><SquareButton size="compact" onClick={() => void commit()} disabled={busy || !selectedPath || !commitMessage.trim()}> <GitCommit size={14}/> Commit</SquareButton></>}</div></div></div>{canManage && <div className="project-workspace-panels"><div><h3><GitBranch size={15}/> New branch</h3><div className="project-inline-form"><input value={newBranch} onChange={event => setNewBranch(event.target.value)} placeholder="feature/agent-change"/><SquareButton size="compact" onClick={() => void createNewBranch()} disabled={busy || !newBranch.trim()}>Create</SquareButton></div></div><div><h3><GitPullRequest size={15}/> Pull request</h3><div className="project-inline-form"><input value={prTitle} onChange={event => setPrTitle(event.target.value)} placeholder="PR title"/><SquareButton size="compact" onClick={() => void createPr()} disabled={busy || !prTitle.trim() || branch === (snapshot.default_branch || defaultBranch)}>Open PR</SquareButton></div><textarea value={prBody} onChange={event => setPrBody(event.target.value)} placeholder="Describe the change (optional)"/></div></div>}{message && <p className="settings-runtime-note">{message}</p>}<div className="project-commit-list"><strong>Recent commits</strong>{snapshot.commits.slice(0, 5).map((item: any) => <div key={item.sha}><GitCommit size={13}/><span>{item.message}</span><small>{item.sha.slice(0, 7)}</small></div>)}</div></section>;
}
