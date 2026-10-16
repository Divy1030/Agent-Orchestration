import type { MouseEventHandler } from 'react';

type AuthModeButtonProps = {
  children: string;
  onClick: MouseEventHandler<HTMLButtonElement>;
};

/** Shared, full-width secondary action used to switch between auth modes. */
export default function AuthModeButton({ children, onClick }: AuthModeButtonProps) {
  return <button className="auth-mode-button" type="button" onClick={onClick}>{children}</button>;
}
