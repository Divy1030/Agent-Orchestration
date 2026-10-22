import { useEffect, useState } from 'react';
import { CheckCircle, GithubLogo, LinkSimple } from '@phosphor-icons/react';
import { api } from '../lib/api';
import { useClerkApp } from './ClerkAuth';
import SquareButton from './SquareButton';

type Props = { workspaceId: string };

export default function GitHubConnectionSettings({ workspaceId }: Props) {
  const { connectGithub } = useClerkApp();
  const [connected, setConnected] = useState<boolean | null>(null);
  const [scopes, setScopes] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');

  const check = async () => {
    if (!workspaceId) return;
    try {
      const result = await api<{ connected: boolean; scopes?: string[]; message?: string }>(`/api/spaces/${workspaceId}/github/preflight`);
      setConnected(result.connected);
      setScopes(result.scopes || []);
      if (!result.connected && result.message) setMessage(result.message);
    } catch (error: any) { setConnected(false); setMessage(error.message); }
  };

  useEffect(() => { void check(); }, [workspaceId]);

  const repositoryAccessReady = connected === true && scopes.includes('repo');

  const connect = async () => {
    setBusy(true);
    try { await connectGithub(); setMessage('Complete GitHub authorization, then return here and click Check connection.'); }
    catch (error: any) { setMessage(error.message || 'Could not open the Clerk account profile.'); }
    finally { setBusy(false); }
  };

  return <section className="workflow-library-card utility-card settings-github-card">
    <div className="library-card-heading"><div><h2>GitHub account</h2><p>Connect GitHub to your Clerk account before authorizing Space repositories.</p></div><GithubLogo size={22}/></div>
    <div className={`settings-github-status ${repositoryAccessReady ? 'connected' : connected !== null ? 'needs-connection' : ''}`}><span>{repositoryAccessReady ? <CheckCircle size={16}/> : <LinkSimple size={16}/>}</span><div><strong>{connected === null ? 'Checking connection…' : repositoryAccessReady ? 'GitHub connected with repository access' : connected ? 'GitHub connected — repository access needed' : 'GitHub connection required'}</strong><small>{connected ? `OAuth scopes: ${scopes.length ? scopes.join(', ') : 'provider account linked'}` : 'Your Google sign-in can stay active. GitHub is linked separately through Clerk.'}</small></div></div>
    <div className="settings-github-actions"><SquareButton onClick={() => void connect()} disabled={busy}>{busy ? 'Opening…' : repositoryAccessReady ? 'Manage GitHub connection' : connected ? 'Reconnect with repository access' : 'Connect GitHub'}</SquareButton><SquareButton tone="quiet" onClick={() => void check()} disabled={busy}>Check connection</SquareButton></div>
    {message && <p className="settings-inline-note">{message}</p>}
  </section>;
}
