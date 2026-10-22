import { useEffect, useState } from 'react';
import { api } from '../lib/api';
import SquareButton from './SquareButton';
import SpaceDetailPage from './SpaceDetailPage';

type Space = { id: string; name: string; slug: string; role: string; join_code?: string };

export default function SpacesPage() {
  const [spaces, setSpaces] = useState<Space[]>([]), [selected, setSelected] = useState<Space | null>(null), [members, setMembers] = useState<any[]>([]);
  const [name, setName] = useState(''), [joinCode, setJoinCode] = useState(''), [ownerCode, setOwnerCode] = useState(''), [message, setMessage] = useState(''), [busy, setBusy] = useState(false);
  const load = async () => { const result = await api<{ workspaces: Space[] }>('/api/workspaces'); setSpaces(result.workspaces); if (selected) await loadMembers(selected.id); };
  const loadMembers = async (id: string) => { const result = await api<{ members: any[] }>(`/api/spaces/${id}/members`); setMembers(result.members); };
  useEffect(() => { void load().catch((error: any) => setMessage(error.message)); }, []);
  const create = async () => { setBusy(true); try { const result = await api<{ workspace: Space }>('/api/workspaces', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) }); setSpaces(current => [...current, result.workspace]); setSelected(result.workspace); setOwnerCode(result.workspace.join_code || ''); setName(''); setMessage('Space created. Share the join code with collaborators.'); } catch (error: any) { setMessage(error.message); } finally { setBusy(false); } };
  const join = async () => { setBusy(true); try { const result = await api<{ space: Space }>('/api/spaces/join', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code: joinCode }) }); setSpaces(current => [...current, result.space]); setJoinCode(''); setMessage('You joined the Space.'); } catch (error: any) { setMessage(error.message); } finally { setBusy(false); } };
  const rotate = async () => { if (!selected) return; setBusy(true); try { const result = await api<{ space: { join_code: string } }>(`/api/spaces/${selected.id}/join-code/rotate`, { method: 'POST' }); setOwnerCode(result.space.join_code); setMessage('Join code rotated. The previous code is no longer valid.'); } catch (error: any) { setMessage(error.message); } finally { setBusy(false); } };
  const select = async (space: Space) => { setSelected(space); setOwnerCode(''); setMessage(''); try { await loadMembers(space.id); } catch (error: any) { setMessage(error.message); } };
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const spaceId = params.get('space_id');
    if (!spaceId || selected || !spaces.length) return;
    const target = spaces.find(space => space.id === spaceId);
    if (target) {
      setMessage(params.get('github') === 'authorized' ? 'GitHub App installed and the selected repository is authorized.' : params.get('message') || 'GitHub authorization finished.');
      void select(target);
    }
  }, [spaces, selected]);
  if (selected) return <SpaceDetailPage spaceId={selected.id} onBack={() => setSelected(null)} />;
  return <section className="spaces-page"><header className="library-heading"><div><div className="library-title"><h1>Spaces</h1></div><p>Collaborate around shared workflows, agents, decisions, and future project rooms.</p></div></header><div className="spaces-layout"><section className="workflow-library-card utility-card"><div className="library-card-heading"><div><h2>Your Spaces</h2><p>Personal workflows remain available without joining a Space.</p></div><span>{spaces.length}</span></div><div className="settings-form"><label>New Space name<input value={name} onChange={event => setName(event.target.value)} placeholder="Product engineering"/></label><SquareButton onClick={() => void create()} disabled={busy || !name.trim()}>Create Space</SquareButton><label>Join with code<input value={joinCode} onChange={event => setJoinCode(event.target.value.toUpperCase())} placeholder="ABCD1234"/></label><SquareButton tone="quiet" onClick={() => void join()} disabled={busy || !joinCode.trim()}>Join Space</SquareButton></div><div className="space-list">{spaces.map(space => <button className="space-list-item" key={space.id} onClick={() => void select(space)}><span><strong>{space.name}</strong><small>{space.role} · {space.slug}</small></span></button>)}{!spaces.length && <p className="inspector-empty">Create a Space when you want to collaborate. Solo workflows do not require one.</p>}</div></section></div>{message && <p className="settings-runtime-note">{message}</p>}</section>;
}
