<script lang="ts">
  let {
    checked = false,
    onchange,
    label = '',
    disabled = false,
    describedby = ''
  }: {
    checked?: boolean;
    onchange?: (next: boolean) => void;
    label?: string;
    disabled?: boolean;
    describedby?: string;
  } = $props();
</script>

<label class="toggle" class:toggle--disabled={disabled}>
  <input
    type="checkbox"
    {checked}
    {disabled}
    aria-label={label}
    aria-describedby={describedby}
    onchange={(e) => onchange?.(e.currentTarget.checked)}
  />
  <span class="toggle-track" aria-hidden="true">
    <span class="toggle-thumb"></span>
  </span>
  {#if label}<span class="toggle-label">{label}</span>{/if}
</label>

<style>
  .toggle {
    display: inline-flex;
    align-items: center;
    gap: var(--sp-2);
    cursor: pointer;
    font-size: var(--fs-md);
    color: var(--text);
    user-select: none;
  }

  .toggle--disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .toggle input {
    position: absolute;
    opacity: 0;
    width: 1px;
    height: 1px;
    margin: 0;
  }

  .toggle-track {
    position: relative;
    flex: none;
    width: 40px;
    height: 22px;
    border-radius: var(--radius-full);
    background: var(--surface-3);
    border: 1px solid var(--border-strong);
    transition: background 0.2s ease;
  }

  .toggle-thumb {
    position: absolute;
    top: 2px;
    left: 2px;
    width: 16px;
    height: 16px;
    border-radius: 50%;
    background: #fff;
    box-shadow: var(--shadow-sm);
    transition: transform 0.2s ease;
  }

  .toggle input:checked + .toggle-track {
    background: var(--success);
    border-color: var(--success);
  }

  .toggle input:checked + .toggle-track .toggle-thumb {
    transform: translateX(18px);
  }

  .toggle input:focus-visible + .toggle-track {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }

  .toggle-label {
    min-width: 0;
  }
</style>
