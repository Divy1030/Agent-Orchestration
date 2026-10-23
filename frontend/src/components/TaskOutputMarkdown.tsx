import { Fragment } from 'react';

function inlineText(value: string) {
  return value.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((part, index) => {
    if (part.startsWith('**') && part.endsWith('**')) return <strong key={index}>{part.slice(2, -2)}</strong>;
    if (part.startsWith('`') && part.endsWith('`')) return <code key={index}>{part.slice(1, -1)}</code>;
    return <Fragment key={index}>{part}</Fragment>;
  });
}

export default function TaskOutputMarkdown({ content }: { content: string }) {
  const lines = content.split(/\r?\n/);
  const blocks: JSX.Element[] = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (line.trim().startsWith('```')) {
      const language = line.trim().slice(3).trim() || 'text';
      const code: string[] = [];
      index += 1;
      while (index < lines.length && !lines[index].trim().startsWith('```')) code.push(lines[index++]);
      const rawCode = code.join('\n');
      const readableCode = rawCode.replace(/\\n/g, '\n').replace(/\\r/g, '\r').replace(/\\t/g, '\t').replace(/\\"/g, '"');
      if (language.toLowerCase() === 'json') {
        try {
          const structured = JSON.parse(rawCode);
          if (Array.isArray(structured.files)) {
            blocks.push(<div className="task-output-proposal" key={`proposal-${index}`}>
              {structured.summary && <p><strong>Summary:</strong> {structured.summary}</p>}
              {structured.files.map((file: { path?: string; content?: string }, fileIndex: number) => <section className="task-output-file" key={file.path || fileIndex}>
                <div className="task-output-file-path">{file.path || 'proposed file'}</div>
                <pre className="task-output-code"><code>{file.content || ''}</code></pre>
              </section>)}
              {Array.isArray(structured.verification) && structured.verification.length > 0 && <p><strong>Verification:</strong> {structured.verification.join(' · ')}</p>}
            </div>);
          } else {
            blocks.push(<pre className="task-output-code" key={`code-${index}`}><span className="task-output-code-label">{language}</span><code>{JSON.stringify(structured, null, 2)}</code></pre>);
          }
        } catch {
          blocks.push(<pre className="task-output-code" key={`code-${index}`}><span className="task-output-code-label">{language}</span><code>{readableCode}</code></pre>);
        }
      } else {
        blocks.push(<pre className="task-output-code" key={`code-${index}`}><span className="task-output-code-label">{language}</span><code>{readableCode}</code></pre>);
      }
      index += 1;
      continue;
    }
    if (!line.trim()) { index += 1; continue; }
    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) { const Tag = heading[1].length === 1 ? 'h3' : 'h4'; blocks.push(<Tag key={`heading-${index}`}>{inlineText(heading[2])}</Tag>); index += 1; continue; }
    if (/^\s*[-*]\s+/.test(line)) {
      const items: string[] = [];
      while (index < lines.length && /^\s*[-*]\s+/.test(lines[index])) items.push(lines[index++].replace(/^\s*[-*]\s+/, ''));
      blocks.push(<ul key={`list-${index}`}>{items.map((item, itemIndex) => <li key={itemIndex}>{inlineText(item)}</li>)}</ul>);
      continue;
    }
    const numbered = line.match(/^\s*\d+\.\s+(.+)$/);
    if (numbered) { blocks.push(<p className="task-output-numbered" key={`number-${index}`}><span>{line.match(/^\s*(\d+)\./)?.[1]}.</span>{inlineText(numbered[1])}</p>); index += 1; continue; }
    blocks.push(<p key={`paragraph-${index}`}>{inlineText(line)}</p>);
    index += 1;
  }
  return <div className="task-output-markdown">{blocks}</div>;
}
