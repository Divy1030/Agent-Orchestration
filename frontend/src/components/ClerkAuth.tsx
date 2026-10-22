import { createContext, useContext, useEffect, useState, type ReactNode } from 'react';
import { ClerkProvider, SignIn, UserButton, useAuth, useClerk, useReverification, useUser } from '@clerk/react';
import { setClerkTokenGetter } from '../lib/api';
import './ClerkAuth.css';

type ClerkAppState = {
  enabled: boolean;
  loaded: boolean;
  signedIn: boolean;
  signOut: () => Promise<void>;
  openUserProfile: () => Promise<void>;
  connectGithub: (spaceId?: string) => Promise<void>;
};

const ClerkAppContext = createContext<ClerkAppState>({
  enabled: false,
  loaded: true,
  signedIn: false,
  signOut: async () => { },
  openUserProfile: async () => { },
  connectGithub: async () => { },
});

const publishableKey = (import.meta.env.VITE_CLERK_PUBLISHABLE_KEY || '').trim();

function ClerkBridge({ children }: { children: ReactNode }) {
  const { isLoaded, isSignedIn, getToken } = useAuth();
  const clerk = useClerk();
  const { user } = useUser();
  const connectGithubOauth = useReverification(async (spaceId?: string) => {
    if (!user) throw new Error('Your Clerk session is still loading. Try again in a moment.');
    const returnTo = spaceId ? `/spaces?github=oauth_connected&space_id=${encodeURIComponent(spaceId)}` : `${window.location.pathname}${window.location.search}`;
    const callback = `${window.location.origin}/sso-callback?return_to=${encodeURIComponent(returnTo)}`;
    // Clerk may complete the external-account flow without preserving the
    // callback query string. Keep the app destination available locally so
    // the callback never falls back to Clerk's hosted /default-redirect.
    sessionStorage.setItem('github_oauth_return_to', returnTo);
    const existing = user.externalAccounts.find(account => String(account.provider).toLowerCase().includes('github'));
    const account = existing
      ? await existing.reauthorize({ additionalScopes: ['repo'], redirectUrl: callback })
      : await user.createExternalAccount({ strategy: 'oauth_github', additionalScopes: ['repo'], redirectUrl: callback });
    const redirect = account?.verification?.externalVerificationRedirectURL;
    if (redirect) window.location.assign(redirect.href);
    return account;
  });

  // Register before rendering App so its initial auth/API effects include the
  // session bearer token. The publishable key is the only client-side key.
  setClerkTokenGetter(isLoaded && isSignedIn ? getToken : null);

  if (!isLoaded) return <div className="auth-loading" role="status">Loading secure sign-in…</div>;
  return <ClerkAppContext.Provider value={{
    enabled: true,
    loaded: isLoaded,
    signedIn: Boolean(isSignedIn),
    signOut: async () => { await clerk.signOut(); },
    openUserProfile: async () => { await clerk.openUserProfile(); },
    connectGithub: async (spaceId?: string) => {
      try {
        await connectGithubOauth(spaceId);
      } catch {
        // Clerk may reject creating a second GitHub external account when one
        // is already linked. The profile reconnect action then applies the
        // additionalOAuthScopes configured on UserButton below.
        await clerk.openUserProfile();
      }
    },
  }}>{children}</ClerkAppContext.Provider>;
}

export function ClerkApplication({ children }: { children: ReactNode }) {
  if (!publishableKey) return <>{children}</>;
  return <ClerkProvider publishableKey={publishableKey} afterSignOutUrl="/">
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

export function ClerkOAuthCallback() {
  const params = new URLSearchParams(window.location.search);
  const returnTo = params.get('return_to') || sessionStorage.getItem('github_oauth_return_to') || '/spaces';
  const clerk = useClerk();
  const [error, setError] = useState('');

  useEffect(() => {
    let navigated = false;
    void clerk.handleRedirectCallback({
      signInFallbackRedirectUrl: returnTo,
      signUpFallbackRedirectUrl: returnTo,
      signInForceRedirectUrl: returnTo,
      signUpForceRedirectUrl: returnTo,
    }, async () => {
      navigated = true;
      window.location.replace(returnTo);
    }).then(() => {
      sessionStorage.removeItem('github_oauth_return_to');
      // External-account verification does not always invoke Clerk's normal
      // sign-in navigation callback. Keep the app in control in that case.
      if (!navigated && window.location.pathname === '/sso-callback') {
        window.location.replace(returnTo);
      }
    }).catch((callbackError: unknown) => {
      setError(callbackError instanceof Error ? callbackError.message : 'GitHub authorization could not be completed.');
    });
  }, [clerk, returnTo]);

  return <div className="auth-loading" role="status">
    {error || 'Completing GitHub authorization…'}
  </div>;
}

export function ClerkAccountButton() {
  return <UserButton
    userProfileProps={{ additionalOAuthScopes: { github: ['repo'] } }}
    appearance={{ elements: { userButtonBox: 'clerk-account-button', userButtonTrigger: 'clerk-account-trigger' } }}
  />;
}
