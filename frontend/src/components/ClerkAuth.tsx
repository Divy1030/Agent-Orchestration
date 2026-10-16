import { createContext, useContext, type ReactNode } from 'react';
import { ClerkProvider, SignIn, UserButton, useAuth, useClerk } from '@clerk/react';
import { setClerkTokenGetter } from '../lib/api';
import './ClerkAuth.css';

type ClerkAppState = {
  enabled: boolean;
  loaded: boolean;
  signedIn: boolean;
  signOut: () => Promise<void>;
};

const ClerkAppContext = createContext<ClerkAppState>({
  enabled: false,
  loaded: true,
  signedIn: false,
  signOut: async () => { },
});

const publishableKey = (import.meta.env.VITE_CLERK_PUBLISHABLE_KEY || '').trim();

function ClerkBridge({ children }: { children: ReactNode }) {
  const { isLoaded, isSignedIn, getToken } = useAuth();
  const clerk = useClerk();

  // Register before rendering App so its initial auth/API effects include the
  // session bearer token. The publishable key is the only client-side key.
  setClerkTokenGetter(isLoaded && isSignedIn ? getToken : null);

  if (!isLoaded) return <div className="auth-loading" role="status">Loading secure sign-in…</div>;
  return <ClerkAppContext.Provider value={{
    enabled: true,
    loaded: isLoaded,
    signedIn: Boolean(isSignedIn),
    signOut: async () => { await clerk.signOut(); },
  }}>{children}</ClerkAppContext.Provider>;
}

export function ClerkApplication({ children }: { children: ReactNode }) {
  if (!publishableKey) return <>{children}</>;
  return <ClerkProvider publishableKey={publishableKey}>
    <ClerkBridge>{children}</ClerkBridge>
  </ClerkProvider>;
}

export function useClerkApp() {
  return useContext(ClerkAppContext);
}

export function ClerkSignInPanel() {
  const { isSignedIn } = useAuth();
  if (isSignedIn) return null;
  const appearance = {
    variables: { colorPrimary: '#6250be', colorText: 'var(--ink)', colorBackground: 'var(--surface)', colorInputBackground: 'var(--surface-muted)', colorInputText: 'var(--ink)', colorTextSecondary: 'var(--subtle)', borderRadius: '0px' as const },
    elements: { card: { boxShadow: 'none', border: '0', padding: '0', width: '100%' }, header: { display: 'none' }, footer: { display: 'none' }, footerAction: { display: 'none' } },
  };
  return <div className="clerk-auth-panel"><SignIn
    routing="hash"
    signUpUrl="/signup-closed"
    forceRedirectUrl="/home"
    appearance={appearance}
  /></div>;
}

export function ClerkAccountButton() {
  return <UserButton
    appearance={{ elements: { userButtonBox: 'clerk-account-button', userButtonTrigger: 'clerk-account-trigger' } }}
    afterSignOutUrl="/"
  />;
}
