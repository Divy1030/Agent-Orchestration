import { useEffect, useSyncExternalStore } from 'react';
import { api } from './api';

type Snapshot = { project: any | null; tasks: any[]; agents: any[]; loading: boolean; error: string };
type Store = { snapshot: Snapshot; listeners: Set<() => void>; timer?: number; inFlight?: Promise<void> };

const stores = new Map<string, Store>();

function getStore(spaceId: string) {
  let store = stores.get(spaceId);
  if (!store) {
    store = { snapshot: { project: null, tasks: [], agents: [], loading: true, error: '' }, listeners: new Set() };
    stores.set(spaceId, store);
  }
  return store;
}

async function refresh(spaceId: string) {
  const store = getStore(spaceId);
  if (store.inFlight) return store.inFlight;
  store.inFlight = Promise.all([
    api<{ project: any }>(`/api/spaces/${spaceId}/project`),
    api<{ tasks: any[] }>(`/api/spaces/${spaceId}/tasks`),
    api<{ agents: any[] }>(`/api/spaces/${spaceId}/agents`),
  ]).then(([projectResult, tasksResult, agentsResult]) => {
    store.snapshot = { project: projectResult.project, tasks: tasksResult.tasks, agents: agentsResult.agents, loading: false, error: '' };
  }).catch((error: any) => {
    store.snapshot = { ...store.snapshot, loading: false, error: error.message || 'Could not refresh project state.' };
  }).finally(() => {
    store.inFlight = undefined;
    store.listeners.forEach(listener => listener());
  });
  return store.inFlight;
}

function startPolling(spaceId: string) {
  const store = getStore(spaceId);
  if (store.timer) return;
  const poll = () => {
    if (document.visibilityState === 'visible') void refresh(spaceId);
    const running = store.snapshot.tasks.some(task => task.status === 'in_progress' || task.execution?.status === 'running' || task.execution?.phase === 'calling_model');
    store.timer = window.setTimeout(poll, running ? 2500 : 8000);
  };
  void refresh(spaceId);
  store.timer = window.setTimeout(poll, 2500);
}

function stopPolling(spaceId: string) {
  const store = getStore(spaceId);
  if (store.listeners.size || !store.timer) return;
  window.clearTimeout(store.timer);
  store.timer = undefined;
}

export function useSpaceProjectStore(spaceId: string) {
  const store = getStore(spaceId);
  const snapshot = useSyncExternalStore(
    listener => {
      store.listeners.add(listener);
      startPolling(spaceId);
      return () => { store.listeners.delete(listener); stopPolling(spaceId); };
    },
    () => store.snapshot,
    () => store.snapshot,
  );
  useEffect(() => { void refresh(spaceId); }, [spaceId]);
  return { ...snapshot, refresh: () => refresh(spaceId) };
}
