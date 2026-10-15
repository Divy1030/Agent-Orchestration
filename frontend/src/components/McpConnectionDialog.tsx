import { useState } from 'react';
import { ArrowClockwise, PlugsConnected, Wrench } from '@phosphor-icons/react';

export type McpRemoteTool = { name: string; description: string; input_schema: Record<string, any> };
export type McpRemoteServer = { name: string; description: string; server_url: string; functions: McpRemoteTool[] };

type Props = {
  onClose: () => void;
  onDiscover: (url: string) => Promise<McpRemoteTool[]>;
  onAdd: (server: McpRemoteServer) => void;
};

export default function McpConnectionDialog({ onClose, onDiscover, onAdd }: Props) {
  const [url, setUrl] = useState('');
  const [serverName, setServerName] = useState('');
  const [functions, setFunctions] = useState<McpRemoteTool[]>([]);
  const [busy, setBusy] = useState(false);
  const [added, setAdded] = useState(false);
  const [error, setError] = useState('');

  async function discover() {
    setBusy(true);
    setError('');
    setFunctions([]);
    setAdded(false);
    try {
      const found = await onDiscover(url.trim());
      setFunctions(found);
      if (!serverName.trim()) {
        try { setServerName(new URL(url.trim()).hostname.replace(/^www\./, '').split('.')[0].replace(/[-_]/g, ' ').replace(/\b\w/g, c => c.toUpperCase())); }
        catch { setServerName('MCP Server'); }
      }
      if (!found.length) setError('Connected, but this server did not expose any functions.');
    } catch (reason: any) {
      setError(reason?.message || 'Could not connect to the MCP server.');
    } finally {
      setBusy(false);
    }
  }

  return <div className="modal-backdrop" onClick={onClose}>
    <section className="node-picker mcp-connect-dialog" onClick={event => event.stopPropagation()}>
      <div className="dialog-heading">
        <div><span className="eyebrow">MODEL CONTEXT PROTOCOL</span><h2>Connect an MCP server</h2><p>One server connection can expose multiple functions to this node.</p></div>
        <button className="dialog-close" onClick={onClose}>×</button>
      </div>
      <label className="mcp-server-url">Streamable HTTP server URL<input value={url} onChange={event => setUrl(event.target.value)} placeholder="https://example.com/mcp" spellCheck={false}/></label>
      <button className="mcp-discover-button" onClick={() => void discover()} disabled={busy || !url.trim()}>{busy ? <ArrowClockwise className="mcp-spin" size={16}/> : <PlugsConnected size={16}/>} {busy ? 'Connecting…' : 'Connect and discover functions'}</button>
      {error && <p className="mcp-discovery-error">{error}</p>}
      {!!functions.length && <>
        <label className="mcp-server-url">MCP server name<input value={serverName} onChange={event => setServerName(event.target.value)} placeholder="EverQ Analytics"/></label>
        <div className="mcp-discovered-tools"><div className="mcp-tools-heading"><strong>Functions on this server</strong><span>{functions.length} found</span></div>{functions.map(fn => <article className="mcp-discovered-tool" key={fn.name}>
          <span className="mcp-discovered-icon"><Wrench size={16}/></span>
          <div className="mcp-discovered-copy"><strong>{fn.name}</strong><p>{fn.description || 'No description provided by this server.'}</p></div>
        </article>)}</div>
        <button className="mcp-discover-button" onClick={() => { onAdd({ name: serverName.trim() || 'MCP Server', description: `${functions.length} functions`, server_url: url.trim(), functions }); setAdded(true); }} disabled={added}>
          <PlugsConnected size={16}/>{added ? 'Server added to node' : `Add server with ${functions.length} functions`}
        </button>
      </>}
      <p className="mcp-transport-note">This creates one server connection on the node; its functions are grouped under that connection. Remote Streamable HTTP only. Local stdio and authenticated/OAuth servers are not supported yet.</p>
    </section>
  </div>;
}
