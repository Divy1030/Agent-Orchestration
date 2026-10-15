import { useEffect, useState } from 'react';

type Props = {
  server: any;
  servers: any[];
  onClose: () => void;
  onSave: (server: any) => void;
  onDelete: (server: any) => void;
};

function functionsFor(server: any) {
  if (Array.isArray(server.functions)) return server.functions;
  // Read older workflows that stored one remote MCP function per attachment.
  return server.mcp_tool_name ? [{ name: server.mcp_tool_name, description: server.description || '', input_schema: server.input_schema || { type: 'object', properties: {} } }] : [];
}

export default function McpToolEditDialog({ server, servers, onClose, onSave, onDelete }: Props) {
  const [selectedId, setSelectedId] = useState(server.tool_instance_id || server.tool_id);
  const selected = servers.find(item => (item.tool_instance_id || item.tool_id) === selectedId) || server;
  const [name, setName] = useState(selected.name || selected.label || 'MCP Server');
  const [description, setDescription] = useState(selected.description || '');
  const [serverUrl, setServerUrl] = useState(selected.server_url || '');
  const [approvalRequired, setApprovalRequired] = useState(selected.approval_required !== false);
  const functions = functionsFor(selected);

  useEffect(() => {
    setName(selected.name || selected.label || 'MCP Server');
    setDescription(selected.description || '');
    setServerUrl(selected.server_url || '');
    setApprovalRequired(selected.approval_required !== false);
  }, [selectedId]);

  function selectServer(id: string) {
    setSelectedId(id);
    const next = servers.find(item => (item.tool_instance_id || item.tool_id) === id);
    if (next) {
      setName(next.name || next.label || 'MCP Server');
      setDescription(next.description || '');
      setServerUrl(next.server_url || '');
      setApprovalRequired(next.approval_required !== false);
    }
  }

  return <div className="modal-backdrop" onClick={onClose}>
    <section className="node-edit-dialog mcp-edit-dialog" onClick={event => event.stopPropagation()}>
      <div className="dialog-heading"><div><span className="eyebrow">MCP SERVER CONFIGURATION</span><h2>{name || 'MCP Server'}</h2><p>Edit this server connection and its available functions.</p></div><button className="dialog-close" onClick={onClose}>×</button></div>
      {servers.length > 1 && <label>MCP server<select value={selectedId} onChange={event => selectServer(event.target.value)}>{servers.map((item, index) => <option key={item.tool_instance_id || `${item.server_url}-${index}`} value={item.tool_instance_id || item.tool_id}>{item.name || item.label || item.server_url}</option>)}</select></label>}
      <label>Server name<input value={name} onChange={event => setName(event.target.value)} placeholder="EverQ Analytics"/></label>
      <label>Description for the agent<textarea value={description} onChange={event => setDescription(event.target.value)} placeholder="What data and functions does this server provide?"/></label>
      <label>Streamable HTTP server URL<input value={serverUrl} onChange={event => setServerUrl(event.target.value)} spellCheck={false}/></label>
      <section className="mcp-function-list"><div className="mcp-tools-heading"><strong>Functions exposed by this server</strong><span>{functions.length}</span></div>{functions.map((fn: any) => <article className="mcp-function-row" key={fn.name}><strong>{fn.name}</strong><span>{fn.description || 'No description provided.'}</span></article>)}</section>
      <label className="tool-approval-toggle"><input type="checkbox" checked={approvalRequired} onChange={event => setApprovalRequired(event.target.checked)}/> Require approval before this server runs</label>
      <p className="dialog-hint">This is one MCP server connection containing the functions listed above. Runtime mode, approval, and the server’s own permissions govern execution.</p>
      <div className="dialog-actions"><button className="delete-node-dialog-button" onClick={() => onDelete(selected)}>Remove server</button><button className="dialog-cancel" onClick={onClose}>Cancel</button><button className="save-button" onClick={() => onSave({...selected,name:name.trim() || 'MCP Server',label:name.trim() || 'MCP Server',description,server_url:serverUrl.trim(),functions,approval_required:approvalRequired,parameters:{...(selected.parameters || {}),operation:'call'},mcp_tool_name:undefined,input_schema:undefined})}>Save server</button></div>
    </section>
  </div>;
}
