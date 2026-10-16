type Props = { clerkConfigured: boolean };
import './AuthenticationSettings.css';

export default function AuthenticationSettings({ clerkConfigured }: Props) {
  return <section className="workflow-library-card utility-card">
    <div className="library-card-heading">
      <div><h2>Authentication</h2><p>Email and social sign-in provider.</p></div>
      <span className={`auth-provider-status ${clerkConfigured ? 'configured' : ''}`}>
        {clerkConfigured ? 'Client configured' : 'Needs client key'}
      </span>
    </div>
    <div className="settings-runtime-row"><span>Email sign-in and recovery</span><b>Clerk</b></div>
    <div className="settings-runtime-row"><span>Google sign-in</span><b>Configure in Clerk</b></div>
    <p className="settings-runtime-note">
      Existing Mongo accounts and workflow data stay in place. Local development allows test registration; deployed registration stays closed unless explicitly enabled on the backend.
    </p>
    <p className="settings-runtime-note">The API also needs CLERK_SECRET_KEY and CLERK_AUTHORIZED_PARTIES in its private environment. Google must be enabled for this app in the Clerk Dashboard.</p>
    {!clerkConfigured && <p className="settings-runtime-note">Add VITE_CLERK_PUBLISHABLE_KEY to the frontend environment to enable Clerk sign-in.</p>}
  </section>;
}
