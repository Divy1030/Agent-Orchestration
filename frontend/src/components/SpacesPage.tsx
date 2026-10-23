import { useEffect, useState } from 'react';
import { ArrowRight, Plus, SquaresFour, UsersThree } from '@phosphor-icons/react';
import { api } from '../lib/api';
import SquareButton from './SquareButton';
import SpaceDetailPage from './SpaceDetailPage';

type Space = { id: string; name: string; slug: string; project_slug?: string; role: string; join_code?: string };

export default function SpacesPage() {
  const [spaces, setSpaces] = useState<Space[]>([]), [selected, setSelected] = useState<Space | null>(null), [members, setMembers] = useState<any[]>([]), [spacesLoaded, setSpacesLoaded] = useState(false), [routeResolved, setRouteResolved] = useState(false);
  const [name, setName] = useState(''), [joinCode, setJoinCode] = useState(''), [ownerCode, setOwnerCode] = useState(''), [message, setMessage] = useState(''), [busy, setBusy] = useState(false), [dialog, setDialog] = useState<'create' | 'join' | null>(null);
  const spacePath = (space: Space) => `/spaces/${encodeURIComponent(space.project_slug || space.slug)}`;
  const load = async () => { const result = await api<{ workspaces: Space[] }>('/api/workspaces'); setSpaces(result.workspaces); setSpacesLoaded(true); if (selected) await loadMembers(selected.id); };
  const loadMembers = async (id: string) => { const result = await api<{ members: any[] }>(`/api/spaces/${id}/members`); setMembers(result.members); };
  useEffect(() => { void load().catch((error: any) => setMessage(error.message)); }, []);
  const create = async () => { setBusy(true); try { const result = await api<{ workspace: Space }>('/api/workspaces', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) }); setSpaces(current => [...current, result.workspace]); setSelected(result.workspace); setOwnerCode(result.workspace.join_code || ''); setName(''); setDialog(null); setMessage('Space created. Share the join code with collaborators.'); } catch (error: any) { setMessage(error.message); } finally { setBusy(false); } };
  const join = async () => { setBusy(true); try { const result = await api<{ space: Space }>('/api/spaces/join', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code: joinCode }) }); setSpaces(current => [...current, result.space]); setJoinCode(''); setDialog(null); setMessage('You joined the Space.'); } catch (error: any) { setMessage(error.message); } finally { setBusy(false); } };
  const rotate = async () => { if (!selected) return; setBusy(true); try { const result = await api<{ space: { join_code: string } }>(`/api/spaces/${selected.id}/join-code/rotate`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ github_collaborator_confirmed: true }) }); setOwnerCode(result.space.join_code); setMessage('Join code rotated. The previous code is no longer valid.'); } catch (error: any) { setMessage(error.message); } finally { setBusy(false); } };
  const select = async (space: Space, updateUrl = true) => { setSelected(space); setOwnerCode(''); setMessage(''); if (updateUrl && window.location.pathname !== spacePath(space)) window.history.pushState({}, '', spacePath(space)); try { await loadMembers(space.id); } catch (error: any) { setMessage(error.message); } };
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const spaceId = params.get('space_id');
    const pathMatch = window.location.pathname.match(/^\/spaces\/([^/]+)/);
    const routeSlug = pathMatch ? decodeURIComponent(pathMatch[1]) : '';
    if (selected || !spacesLoaded) return;
    const target = spaces.find(space => space.id === spaceId || (space.project_slug || space.slug) === routeSlug || space.slug === routeSlug);
    if (target) {
      setMessage(params.get('github') === 'authorized' ? 'GitHub App installed and the selected repository is authorized.' : params.get('message') || 'GitHub authorization finished.');
      const isWorkRoute = /\/spaces\/[^/]+\/(?:project|work)$/.test(window.location.pathname);
      const canonicalPath = `${spacePath(target)}${isWorkRoute ? '/work' : ''}${window.location.search}`;
      if (window.location.pathname + window.location.search !== canonicalPath) window.history.replaceState({}, '', canonicalPath);
      void select(target, false);
      setRouteResolved(true);
    } else if (routeSlug) {
      setMessage('Space not found or you do not have access to it.');
      setRouteResolved(true);
    } else {
      setRouteResolved(true);
    }
  }, [spaces, selected, spacesLoaded]);
  useEffect(() => {
    const onPopState = () => {
      const match = window.location.pathname.match(/^\/spaces\/([^/]+)/);
      const routeSlug = match ? decodeURIComponent(match[1]) : '';
      const target = match ? spaces.find(space => (space.project_slug || space.slug) === routeSlug || space.slug === routeSlug) : null;
      if (target) {
        const isWorkRoute = /\/spaces\/[^/]+\/(?:project|work)$/.test(window.location.pathname);
        const canonicalPath = `${spacePath(target)}${isWorkRoute ? '/work' : ''}${window.location.search}`;
        if (window.location.pathname + window.location.search !== canonicalPath) window.history.replaceState({}, '', canonicalPath);
        void select(target, false);
      } else { setSelected(null); setOwnerCode(''); }
    };
    window.addEventListener('popstate', onPopState);
    return () => window.removeEventListener('popstate', onPopState);
  }, [spaces]);
  if (!routeResolved) return <section className="spaces-page"><p className="inspector-empty">Loading Space…</p></section>;
  if (selected) return <SpaceDetailPage spaceId={selected.id} onBack={() => { window.history.pushState({}, '', '/spaces'); setSelected(null); setRouteResolved(true); }} />;
  return <section className="workflow-library spaces-page"><header className="library-heading"><div><div className="library-title"><span className="library-symbol"><SquaresFour size={28} weight="bold"/></span><h1>Spaces</h1><span className="book-symbol"><UsersThree size={18}/></span></div><p>Collaborate around shared workflows, agents, decisions, and project rooms.</p></div><div className="spaces-page-actions"><SquareButton onClick={() => { setMessage(''); setDialog('create'); }}><Plus size={17}/> Create new</SquareButton><SquareButton tone="quiet" onClick={() => { setMessage(''); setDialog('join'); }}>Join workspace</SquareButton></div></header><section className="workflow-library-card spaces-library-card"><div className="library-card-heading"><div><h2>Your Spaces</h2><p>Personal workflows remain available without joining a Space.</p></div><span>{spaces.length} space{spaces.length === 1 ? '' : 's'}</span></div><div className="workflow-card-grid spaces-card-grid">{spaces.map(space => <button type="button" className="workflow-list-card space-list-card" key={space.id} onClick={() => void select(space)}><div className="workflow-card-top"><div className="workflow-card-icons"><span><SquaresFour size={20}/></span><span><UsersThree size={20}/></span></div><ArrowRight size={18} className="space-card-arrow"/></div><div className="workflow-card-status"><span className="connected">{space.role === 'owner' ? 'Owner' : 'Member'}</span></div><h2>{space.name}</h2><p>Shared workflows, agents, and project context.</p><div className="workflow-card-footer"><span>{space.project_slug || space.slug}</span><span>Enter space</span></div></button>)}{!spaces.length && <div className="spaces-empty-state"><span className="overview-row-icon"><SquaresFour size={19}/></span><div><strong>No Spaces yet</strong><p>Create a Space when you want to collaborate. Solo workflows do not require one.</p></div><SquareButton size="compact" onClick={() => setDialog('create')}>Create your first Space</SquareButton></div>}</div></section>{message && <p className="settings-runtime-note">{message}</p>}{dialog && <div className="modal-backdrop" onClick={() => setDialog(null)}><section className="agent-dialog spaces-dialog" onClick={event => event.stopPropagation()}><div className="dialog-heading"><div><span className="eyebrow">{dialog === 'create' ? 'NEW SPACE' : 'JOIN SPACE'}</span><h2>{dialog === 'create' ? 'Create a Space' : 'Join a Space'}</h2><p>{dialog === 'create' ? 'Give your collaborative project a clear home.' : 'Use the join code shared by a Space owner.'}</p></div><button type="button" className="dialog-close" aria-label="Close" onClick={() => setDialog(null)}>×</button></div>{dialog === 'create' ? <label>Space name<input autoFocus value={name} onChange={event => setName(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && name.trim()) void create(); }} placeholder="Product engineering"/></label> : <label>Join code<input autoFocus value={joinCode} onChange={event => setJoinCode(event.target.value.toUpperCase())} onKeyDown={event => { if (event.key === 'Enter' && joinCode.trim()) void join(); }} placeholder="ABCD1234"/></label>}<div className="dialog-actions"><SquareButton tone="quiet" onClick={() => setDialog(null)}>Cancel</SquareButton><SquareButton onClick={() => void (dialog === 'create' ? create() : join())} disabled={busy || !(dialog === 'create' ? name.trim() : joinCode.trim())}>{busy ? 'Please wait…' : dialog === 'create' ? 'Create Space' : 'Join Space'}</SquareButton></div></section></div>}</section>;
}
