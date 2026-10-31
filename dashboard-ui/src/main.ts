import './lib/app.css';
import { mount } from 'svelte';
import App from './App.svelte';
import { applyTheme, theme } from './lib/theme.svelte';
import { startRealtime } from './lib/realtime.svelte';

// Appliqué AVANT le premier rendu : pas de flash de thème.
applyTheme(theme.current);

// Un seul coordinateur SSE pour tout le tableau de bord.
startRealtime();

const app = mount(App, {
  target: document.getElementById('app') as HTMLElement
});

export default app;
