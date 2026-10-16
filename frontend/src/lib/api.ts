const API_BASE_URL = (import.meta.env.DEV ? (import.meta.env.VITE_API_BASE_URL || '') : '').replace(/\/+$/, '');
let clerkTokenGetter: (() => Promise<string | null>) | null = null;

export function setClerkTokenGetter(getter: (() => Promise<string | null>) | null) {
  clerkTokenGetter = getter;
}

export async function api<T = any>(url: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  const token = await clerkTokenGetter?.();
  if (token) headers.set('Authorization', `Bearer ${token}`);
  const response = await fetch(`${API_BASE_URL}${url}`, { ...init, headers, credentials: 'include' });
  const text = await response.text();
  let body: any;
  try { body = text ? JSON.parse(text) : {}; } catch { body = { detail: text || 'Request failed' }; }
  if (!response.ok) throw Error(typeof body.detail === 'string' ? body.detail : 'Request failed');
  return body as T;
}
