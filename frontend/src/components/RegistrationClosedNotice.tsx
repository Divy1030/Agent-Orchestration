type Props = { onBackToSignIn: () => void };

export default function RegistrationClosedNotice({ onBackToSignIn }: Props) {
  return <section className="registration-closed" role="status">
    <span className="eyebrow">SIGN-UP</span>
    <h1>Registration is in progress</h1>
    <p>New account creation is temporarily closed while we finish setting things up. Existing users can still sign in.</p>
    <button className="auth-switch" type="button" onClick={onBackToSignIn}>Back to sign in</button>
  </section>;
}
