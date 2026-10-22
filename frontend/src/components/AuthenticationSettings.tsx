type Props = { clerkConfigured: boolean };
import './AuthenticationSettings.css';
import WorkspaceSettings from './WorkspaceSettings';

export default function AuthenticationSettings({ clerkConfigured }: Props) {
  return <WorkspaceSettings workspace={{ id: '' }} />;
}
