type Props = { clerkConfigured: boolean; workspace?: { id: string } | null };
import './AuthenticationSettings.css';
import WorkspaceSettings from './WorkspaceSettings';

export default function AuthenticationSettings({ workspace }: Props) {
  return <WorkspaceSettings workspace={{ id: workspace?.id || '' }} />;
}
