import type { ButtonHTMLAttributes, ReactNode } from 'react';

type Props = ButtonHTMLAttributes<HTMLButtonElement> & {
  children: ReactNode;
  tone?: 'primary' | 'quiet' | 'danger';
  size?: 'compact' | 'regular';
};

export default function SquareButton({ children, tone = 'primary', size = 'regular', className = '', ...props }: Props) {
  return <button {...props} className={`square-button square-button-${tone} square-button-${size} ${className}`.trim()}>{children}</button>;
}
