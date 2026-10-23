import { Fragment, useEffect, useRef, useState, type ReactNode } from 'react';
import { CaretDown, CaretRight, FileCode, FileCss, FileHtml, FileJs, FileText, FileTs, Folder, FolderOpen, GitBranch, GitCommit, GitPullRequest } from '@phosphor-icons/react';
import { api } from '../lib/api';
import SquareButton from './SquareButton';

type Props = { spaceId: string; defaultBranch: string; canManage: boolean; canCommit: boolean };
type TreeNode = { name: string; path: string; type: 'folder' | 'file'; children: TreeNode[] };

function buildFileTree(files: any[]): TreeNode[] {
  const root: TreeNode[] = [];
  for (const file of files) {
    let nodes = root;
    let currentPath = '';
    const parts = String(file.path).split('/').filter(Boolean);
    parts.forEach((name, index) => {
      currentPath = currentPath ? `${currentPath}/${name}` : name;
      const type = index === parts.length - 1 ? 'file' : 'folder';
      let node = nodes.find(item => item.name === name && item.type === type);
      if (!node) {
        node = { name, path: currentPath, type, children: [] };
        nodes.push(node);
      }
      nodes = node.children;
    });
  }
  const sort = (nodes: TreeNode[]): TreeNode[] => nodes.sort((a, b) => a.type === b.type ? a.name.localeCompare(b.name) : a.type === 'folder' ? -1 : 1).map(node => ({ ...node, children: sort(node.children) }));
  return sort(root);
}

function escapeCode(value: string) {
  return value.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function highlightCode(value: string) {
  const tokenPattern = /("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|\/\/[^\n]*|#[^\n]*|\b(?:const|let|var|function|return|if|else|for|while|class|interface|type|import|from|export|default|async|await|true|false|null|undefined|new|try|catch|throw)\b|\b\d+(?:\.\d+)?\b)/g;
  const keywords = new Set(['const', 'let', 'var', 'function', 'return', 'if', 'else', 'for', 'while', 'class', 'interface', 'type', 'import', 'from', 'export', 'default', 'async', 'await', 'true', 'false', 'null', 'undefined', 'new', 'try', 'catch', 'throw']);
  let output = '', cursor = 0;
  for (const match of value.matchAll(tokenPattern)) {
    const token = match[0];
    const index = match.index ?? cursor;
    output += escapeCode(value.slice(cursor, index));
    const className = token.startsWith('//') || token.startsWith('#') ? 'code-token-comment' : token.startsWith('"') || token.startsWith("'") || token.startsWith('`') ? 'code-token-string' : keywords.has(token) ? 'code-token-keyword' : 'code-token-number';
    output += `<span class="${className}">${escapeCode(token)}</span>`;
    cursor = index + token.length;
  }
  return `${output}${escapeCode(value.slice(cursor))}\n`;
}

type DiffRow = { kind: 'same' | 'changed' | 'added' | 'removed'; left?: string; right?: string; leftNo?: number; rightNo?: number };

function buildDiffRows(before: string, after: string): DiffRow[] {
  const left = before.split('\n');
  const right = after.split('\n');
  const table = Array.from({ length: left.length + 1 }, () => new Array<number>(right.length + 1).fill(0));
  for (let i = left.length - 1; i >= 0; i -= 1) for (let j = right.length - 1; j >= 0; j -= 1) table[i][j] = left[i] === right[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
  const raw: Array<{ type: 'same' | 'added' | 'removed'; value: string; line: number }> = [];
  let i = 0, j = 0;
  while (i < left.length || j < right.length) {
    if (i < left.length && j < right.length && left[i] === right[j]) { raw.push({ type: 'same', value: left[i], line: i + 1 }); i += 1; j += 1; }
    else if (j < right.length && (i === left.length || table[i][j + 1] >= table[i + 1][j])) { raw.push({ type: 'added', value: right[j], line: j + 1 }); j += 1; }
    else { raw.push({ type: 'removed', value: left[i], line: i + 1 }); i += 1; }
  }
  const rows: DiffRow[] = [];
  for (let cursor = 0; cursor < raw.length;) {
    if (raw[cursor].type === 'same') { rows.push({ kind: 'same', left: raw[cursor].value, right: raw[cursor].value, leftNo: raw[cursor].line, rightNo: raw[cursor].line }); cursor += 1; continue; }
    const block: typeof raw = [];
    while (cursor < raw.length && raw[cursor].type !== 'same') { block.push(raw[cursor]); cursor += 1; }
    const removed = block.filter(item => item.type === 'removed');
    const added = block.filter(item => item.type === 'added');
    for (let index = 0; index < Math.max(removed.length, added.length); index += 1) rows.push({ kind: removed[index] && added[index] ? 'changed' : removed[index] ? 'removed' : 'added', left: removed[index]?.value, leftNo: removed[index]?.line, right: added[index]?.value, rightNo: added[index]?.line });
  }
  return rows;
}

function FileTypeIcon({ path }: { path: string }) {
  const extension = path.split('.').pop()?.toLowerCase();
  if (extension === 'ts' || extension === 'tsx') return <FileTs size={14}/>;
  if (extension === 'js' || extension === 'jsx') return <FileJs size={14}/>;
  if (extension === 'css' || extension === 'scss') return <FileCss size={14}/>;
  if (extension === 'html') return <FileHtml size={14}/>;
  if (['md', 'txt', 'json', 'yml', 'yaml', 'env'].includes(extension || '')) return <FileText size={14}/>;
  return <FileCode size={14}/>;
}

export default function SpaceProjectWorkspace({ spaceId, defaultBranch, canManage, canCommit }: Props) {
  const [branch, setBranch] = useState(defaultBranch || 'main');
  const [snapshot, setSnapshot] = useState<any>(null), [selectedPath, setSelectedPath] = useState(''), [content, setContent] = useState('');
  const [newBranch, setNewBranch] = useState(''), [commitMessage, setCommitMessage] = useState(''), [prTitle, setPrTitle] = useState(''), [prBody, setPrBody] = useState('');
  const [message, setMessage] = useState(''), [busy, setBusy] = useState(false), [loadingFile, setLoadingFile] = useState(false), [loadingSnapshot, setLoadingSnapshot] = useState(false), [expandedFolders, setExpandedFolders] = useState<Record<string, boolean>>({}), [editing, setEditing] = useState(false), [previewing, setPreviewing] = useState(false), [originals, setOriginals] = useState<Record<string, string>>({}), [drafts, setDrafts] = useState<Record<string, string>>({});
  const [recentCommitsHeight, setRecentCommitsHeight] = useState(220), [resizingRecent, setResizingRecent] = useState(false);
  const recentDragStart = useRef<{ y: number; height: number } | null>(null);
  const draftStorageKey = `space-workspace-drafts:${spaceId}:${branch}`;
  const [hydratedDraftKey, setHydratedDraftKey] = useState<string | null>(null);

  const loadSnapshot = async (selectedBranch = branch) => {
    setLoadingSnapshot(true); setMessage(`⟳ Loading ${selectedBranch}…`);
    try { const result = await api<{ snapshot: any }>(`/api/spaces/${spaceId}/github/snapshot?branch=${encodeURIComponent(selectedBranch)}`); setSnapshot(result.snapshot); setBranch(result.snapshot.branch); setMessage(''); } finally { setLoadingSnapshot(false); }
  };
  useEffect(() => { void loadSnapshot().catch((error: any) => setMessage(error.message)); }, [spaceId]);
  useEffect(() => {
    setHydratedDraftKey(null);
    try {
      const saved = sessionStorage.getItem(draftStorageKey);
      const parsed = saved ? JSON.parse(saved) as { drafts?: Record<string, string>; originals?: Record<string, string> } : {};
      setDrafts(parsed.drafts || {});
      setOriginals(parsed.originals || {});
    } catch {
      setDrafts({});
      setOriginals({});
    } finally {
      setHydratedDraftKey(draftStorageKey);
    }
  }, [draftStorageKey]);
  useEffect(() => {
    if (hydratedDraftKey !== draftStorageKey) return;
    try {
      if (Object.keys(drafts).length) sessionStorage.setItem(draftStorageKey, JSON.stringify({ drafts, originals }));
      else sessionStorage.removeItem(draftStorageKey);
    } catch {
      setMessage('Temporary draft storage is unavailable; commit your changes before refreshing.');
    }
  }, [draftStorageKey, drafts, originals, hydratedDraftKey]);
  useEffect(() => {
    if (!selectedPath || !editing || originals[selectedPath] === undefined) return;
    setDrafts(current => {
      const next = { ...current };
      if (content === originals[selectedPath]) delete next[selectedPath];
      else next[selectedPath] = content;
      return next;
    });
  }, [content, editing, originals, selectedPath]);
  useEffect(() => {
    if (!resizingRecent) return;
    const move = (event: PointerEvent) => {
      const start = recentDragStart.current;
      if (!start) return;
      setRecentCommitsHeight(Math.max(170, Math.min(520, start.height + start.y - event.clientY)));
    };
    const stop = () => { setResizingRecent(false); recentDragStart.current = null; };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', stop);
    return () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', stop); };
  }, [resizingRecent]);
  useEffect(() => {
    const start = (event: PointerEvent) => {
      const target = event.target as HTMLElement;
      const box = target.closest('.project-recent-commits') as HTMLElement | null;
      if (!box || event.clientY > box.getBoundingClientRect().top + 8) return;
      recentDragStart.current = { y: event.clientY, height: recentCommitsHeight };
      setResizingRecent(true);
      event.preventDefault();
    };
    document.addEventListener('pointerdown', start);
    return () => document.removeEventListener('pointerdown', start);
  }, [recentCommitsHeight]);

  const openFile = async (path: string) => {
    setLoadingFile(true); setMessage(`⟳ Loading ${path}…`);
    try { if (Object.prototype.hasOwnProperty.call(drafts, path)) { setSelectedPath(path); setContent(drafts[path]); setEditing(false); setPreviewing(false); setMessage(''); return; } const result = await api<{ file: { content: string } }>(`/api/spaces/${spaceId}/github/file?path=${encodeURIComponent(path)}&branch=${encodeURIComponent(branch)}`); setSelectedPath(path); setContent(result.file.content); setOriginals(current => ({ ...current, [path]: result.file.content })); setEditing(false); setPreviewing(false); setMessage(''); } catch (error: any) { setMessage(error.message); } finally { setLoadingFile(false); }
  };
  const createNewBranch = async () => {
    setBusy(true);
    try { await api(`/api/spaces/${spaceId}/github/branches`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ branch: newBranch, from_branch: branch }) }); setBranch(newBranch); setNewBranch(''); setMessage(`Branch ${newBranch} created.`); await loadSnapshot(newBranch); } catch (error: any) { setMessage(error.message); } finally { setBusy(false); }
  };
  const commit = async () => {
    if (!canCommit) { setMessage('Only the Space owner can commit directly. Create a pull request instead.'); return; }
    setBusy(true);
    try { const result = await api<{ commit: { commit_sha: string } }>(`/api/spaces/${spaceId}/github/commits`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ branch, message: commitMessage, files: [{ path: selectedPath, content }] }) }); setCommitMessage(''); setOriginals(current => ({ ...current, [selectedPath]: content })); setDrafts(current => { const next = { ...current }; delete next[selectedPath]; return next; }); setEditing(false); setMessage(`Committed ${result.commit.commit_sha.slice(0, 7)} on ${branch}.`); await loadSnapshot(); } catch (error: any) { setMessage(error.message); } finally { setBusy(false); }
  };
  const createPr = async () => {
    setBusy(true);
    try { const result = await api<{ pull_request: { url: string } }>(`/api/spaces/${spaceId}/github/pull-requests`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: prTitle, body: prBody, head: branch, base: snapshot?.default_branch || defaultBranch }) }); setPrTitle(''); setPrBody(''); setMessage(`Pull request created: ${result.pull_request.url}`); } catch (error: any) { setMessage(error.message); } finally { setBusy(false); }
  };

  if (!snapshot) return <section className="space-project-workspace"><p className="inspector-empty">Loading repository…</p>{message && <p className="settings-runtime-note">{message}</p>}</section>;
  const files = snapshot.tree.filter((item: any) => item.type === 'blob');
  const changedFiles = Object.keys(drafts).filter(path => drafts[path] !== originals[path]);
  const diffRows = selectedPath && originals[selectedPath] !== undefined ? buildDiffRows(originals[selectedPath], content) : [];
  const tree = buildFileTree(files);
  const renderTree = (nodes: TreeNode[], depth = 0): ReactNode[] => nodes.map(node => node.type === 'folder' ? <Fragment key={node.path}><button type="button" className="project-tree-row project-tree-folder" style={{ paddingLeft: `${8 + depth * 16}px` }} onClick={() => setExpandedFolders(current => ({ ...current, [node.path]: !(current[node.path] ?? true) }))}>{(expandedFolders[node.path] ?? true) ? <CaretDown size={13}/> : <CaretRight size={13}/>} {(expandedFolders[node.path] ?? true) ? <FolderOpen size={15}/> : <Folder size={15}/>}<span>{node.name}</span></button>{(expandedFolders[node.path] ?? true) && renderTree(node.children, depth + 1)}</Fragment> : <button type="button" className={`project-tree-row project-tree-file ${selectedPath === node.path ? 'selected' : ''}`} style={{ paddingLeft: `${25 + depth * 16}px` }} key={node.path} onClick={() => void openFile(node.path)}><FileTypeIcon path={node.path}/><span>{node.name}</span>{drafts[node.path] !== undefined && drafts[node.path] !== originals[node.path] && <b className="git-status git-status-modified">M</b>}</button>);
  const diffPreview = <div className="project-diff-preview">{diffRows.map((row, index) => <div className={`project-diff-row ${row.kind}`} key={`${row.leftNo || 'x'}-${row.rightNo || 'x'}-${index}`}><span className="project-diff-line-number">{row.leftNo || ''}</span><code dangerouslySetInnerHTML={{ __html: row.left === undefined ? '' : highlightCode(row.left) }}/><span className="project-diff-line-number">{row.rightNo || ''}</span><code dangerouslySetInnerHTML={{ __html: row.right === undefined ? '' : highlightCode(row.right) }}/></div>)}</div>;
  return <section className="space-project-workspace"><div className="project-workspace-toolbar"><div><span className="eyebrow">REPOSITORY WORKSPACE</span><h2>{snapshot.full_name}</h2><p>Review files, work on an isolated branch, then open a pull request.</p></div><label className="branch-picker"><GitBranch size={15}/><select value={branch} onChange={event => { setBranch(event.target.value); void loadSnapshot(event.target.value).catch((error: any) => setMessage(error.message)); }}>{[snapshot.default_branch, branch].filter((item, index, all) => item && all.indexOf(item) === index).map(item => <option key={item}>{item}</option>)}</select></label></div><div className="project-workspace-grid"><aside className="project-file-tree"><strong>Explorer</strong>{renderTree(tree)}{!files.length && <p>No text files found.</p>}<section className="project-changes-panel"><div className="project-changes-heading"><span>Changes</span><b>{changedFiles.length}</b></div>{canCommit && <div className="project-changes-commit"><input value={commitMessage} onChange={event => setCommitMessage(event.target.value)} placeholder="Message (Ctrl+Enter to commit)"/><SquareButton size="compact" onClick={() => void commit()} disabled={busy || !selectedPath || !commitMessage.trim()}>Commit</SquareButton></div>}<div className="project-change-list">{changedFiles.map(path => <button type="button" className="project-change-row" key={path} onClick={() => void openFile(path)}><FileTypeIcon path={path}/><span>{path.split("/").pop()}</span><small>{path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "workspace"}</small><b className="git-status-modified">M</b></button>)}{!changedFiles.length && <p className="project-no-changes">No local changes</p>}</div><section className="project-recent-commits"><div className="project-recent-heading">Recent commits</div><div className="project-branch-label"><GitBranch size={13}/><span>origin/{branch}</span></div>{snapshot.commits.slice(0, 20).map((item: any, index: number) => <div className="project-commit-graph-row" key={item.sha}><span className={`project-commit-dot ${index < 3 ? "blue" : "orange"}`}/><span className="project-commit-message">{item.message}</span><small>{item.sha.slice(0, 7)}</small></div>)}</section></section></aside><div className="project-editor"><div className="project-editor-heading"><span>{selectedPath || 'Select a file'}</span><div className="project-editor-heading-actions">{selectedPath && <span>{content.length.toLocaleString()} chars</span>}{selectedPath && changedFiles.includes(selectedPath) && <SquareButton size="compact" onClick={() => setPreviewing(current => !current)}>{previewing ? 'Edit view' : 'Preview'}</SquareButton>}{selectedPath && canManage && <SquareButton size="compact" onClick={() => { setEditing(current => !current); setPreviewing(false); }}>{editing ? 'Done' : 'Edit'}</SquareButton>}</div></div>{previewing ? diffPreview : editing ? <textarea className="project-code-input" value={content} onChange={event => setContent(event.target.value)} placeholder="Select a file to inspect it…" spellCheck={false}/> : <pre className="project-code-viewer" dangerouslySetInnerHTML={{ __html: selectedPath ? highlightCode(content) : 'Select a file to inspect it…' }}/>}<div className="project-editor-actions">{canCommit && <><input value={commitMessage} onChange={event => setCommitMessage(event.target.value)} placeholder="Commit message"/><SquareButton size="compact" onClick={() => void commit()} disabled={busy || !selectedPath || !commitMessage.trim()}><GitCommit size={14}/> Commit</SquareButton></>}</div></div></div>{canManage && <div className="project-workspace-panels"><div><h3><GitBranch size={15}/> New branch</h3><div className="project-inline-form"><input value={newBranch} onChange={event => setNewBranch(event.target.value)} placeholder="feature/agent-change"/><SquareButton size="compact" onClick={() => void createNewBranch()} disabled={busy || !newBranch.trim()}>Create</SquareButton></div></div><div><h3><GitPullRequest size={15}/> Pull request</h3><div className="project-inline-form"><input value={prTitle} onChange={event => setPrTitle(event.target.value)} placeholder="PR title"/><SquareButton size="compact" onClick={() => void createPr()} disabled={busy || !prTitle.trim() || branch === (snapshot.default_branch || defaultBranch)}>Open PR</SquareButton></div><textarea value={prBody} onChange={event => setPrBody(event.target.value)} placeholder="Describe the change (optional)"/></div></div>}{message && <p className="settings-runtime-note">{message}</p>}<div className="project-commit-list"><strong>Recent commits</strong>{snapshot.commits.slice(0, 5).map((item: any) => <div key={item.sha}><GitCommit size={13}/><span>{item.message}</span><small>{item.sha.slice(0, 7)}</small></div>)}</div></section>;
}
