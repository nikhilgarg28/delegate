import { useState, useEffect, useRef } from "preact/hooks";
import { filterCommands, parseCommand } from "../commands.js";

/**
 * Autocomplete dropdown for magic commands.
 * Appears above the input when in command mode.
 * @param {Object} props
 * @param {string} props.input - Current input value
 * @param {Function} props.onSelect - Called when a command is selected
 * @param {Function} props.onDismiss - Called when autocomplete should close
 */
export function CommandAutocomplete({ input, onSelect, onDismiss }) {
  const [selectedIndex, setSelectedIndex] = useState(0);
  const dropdownRef = useRef();

  const parsed = parseCommand(input);
  const query = parsed ? parsed.name : '';
  const commands = filterCommands(query);

  useEffect(() => {
    setSelectedIndex(0);
  }, [query]);

  useEffect(() => {
    const handleKeyDown = (e) => {
      if (!commands.length) return;

      switch (e.key) {
        case 'ArrowDown':
          e.preventDefault();
          setSelectedIndex(i => (i + 1) % commands.length);
          break;
        case 'ArrowUp':
          e.preventDefault();
          setSelectedIndex(i => (i - 1 + commands.length) % commands.length);
          break;
        case 'Tab':
        case 'Enter':
          if (commands[selectedIndex]) {
            e.preventDefault();
            onSelect(commands[selectedIndex]);
          }
          break;
        case 'Escape':
          e.preventDefault();
          onDismiss();
          break;
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [commands, selectedIndex, onSelect, onDismiss]);

  if (!commands.length) return null;

  return (
    <div class="command-autocomplete" ref={dropdownRef}>
      {commands.map((cmd, idx) => (
        <div
          key={cmd.name}
          class={`command-autocomplete-item ${idx === selectedIndex ? 'selected' : ''}`}
          onClick={() => onSelect(cmd)}
        >
          <span class="command-name">/{cmd.name}</span>
          <span class="command-description">{cmd.description}</span>
        </div>
      ))}
    </div>
  );
}
