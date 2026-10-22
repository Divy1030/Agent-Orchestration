import { useEffect, useState } from 'react';
import { api } from '../lib/api';
import SquareButton from './SquareButton';

type Props = { workspace: { id: string; name: string; role: string }; email?: string; onSaved?: (workspace: any) => void };

export default function WorkspaceDetails({ workspace, email = '', onSaved }: Props) {
  const [editing, setEditing] = useState(false), [name, setName] = useState(workspace.name), [role, setRole] = useState(workspace.role), [saving, setSaving] = useState(false), [error, setError] = useState('');
  useEffect(() => { setName(workspace.name); setRole(workspace.role); }, [workspace.name, workspace.role]);
  const save = async () => { setSaving(true); setError(''); try { const result = await api<{ workspace: any }>(`/api/workspaces/${workspace.id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, role }) }); onSaved?.(result.workspace); setEditing(false); } catch (err: any) { setError(err.message); } finally { setSaving(false); } };
  return <section className="workflow-library-card utility-card settings-workspace-card"><div className="library-card-heading"><div><h2>Workspace</h2><p>Your account and workspace context.</p></div></div><div className="settings-form"><label>Workspace name<input value={name} onChange={event => setName(event.target.value)} readOnly={!editing}/></label><label>Signed-in email<input value={email} readOnly/></label><label>Workspace role<select value={role} onChange={event => setRole(event.target.value)} disabled={!editing}><option value="owner">Owner</option><option value="admin">Admin</option><option value="member">Member</option></select></label></div>{error && <p className="settings-inline-error">{error}</p>}<div className="settings-card-actions">{editing ? <><SquareButton tone="quiet" onClick={() => { setName(workspace.name); setRole(workspace.role); setEditing(false); }}>Cancel</SquareButton><SquareButton onClick={() => void save()} disabled={saving}>{saving ? 'Saving…' : 'Save details'}</SquareButton></> : <SquareButton onClick={() => setEditing(true)}>Edit details</SquareButton>}</div></section>;
}
