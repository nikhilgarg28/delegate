import { useState, useEffect, useRef } from "preact/hooks";
import { filterCommands, parseCommand } from "../commands.js";

/**
 * Autocomplete dropdown for magic commands.
 * Appears inline at the cursor position when in command mode.
 * @param {Object} props
 * @param {string} props.input - Current input value
 * @param {Object} props.inputRef - Ref to the contenteditable input element
 * @param {Function} props.onSelect - Called when a command is selected
 * @param {Function} props.onDismiss - Called when autocomplete should close
 */
export function CommandAutocomplete({ input, inputRef, onSelect, onDismiss }) {
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [position, setPosition] = useState({ top: 0, left: 0 });
  const dropdownRef = useRef();

  const parsed = parseCommand(input);
  const query = parsed ? parsed.name : '';
  const commands = filterCommands(query);

  useEffect(() => {
    setSelectedIndex(0);
  }, [query]);

  // Calculate cursor position for inline dropdown
  useEffect(() => {
    if (!inputRef.current || !commands.length) return;

    const selection = window.getSelection();
    if (!selection.rangeCount) return;

    const range = selection.getRangeAt(0);
    const rect = range.getBoundingClientRect();
    const inputRect = inputRef.current.getBoundingClientRect();

    // Position dropdown at cursor
    // Check if there's more room below or above the cursor
    const spaceBelow = window.innerHeight - rect.bottom;
    const spaceAbove = rect.top;

    let top, left;
    if (spaceBelow >= 200 || spaceBelow > spaceAbove) {
      // Show below cursor
      top = rect.bottom - inputRect.top + 4;
    } else {
      // Show above cursor (dropdown will use max-height and position from bottom)
      top = rect.top - inputRect.top - 4;
    }
    left = rect.left - inputRect.left;

    setPosition({ top, left, showAbove: spaceBelow < 200 && spaceAbove > spaceBelow });
  }, [inputRef, commands, input]);

  useEffect(() => {
    const handleKeyDown = (e) => {
      if (!commands.length) return;

      switch (e.key) {
        case 'ArrowDown':
          e.preventDefault();
          e.stopPropagation();
          setSelectedIndex(i => (i + 1) % commands.length);
          break;
        case 'ArrowUp':
          e.preventDefault();
          e.stopPropagation();
          setSelectedIndex(i => (i - 1 + commands.length) % commands.length);
          break;
        case 'Tab':
        case 'Enter':
          if (commands[selectedIndex]) {
            e.preventDefault();
            e.stopPropagation();
            onSelect(commands[selectedIndex]);
          }
          break;
        case 'Escape':
          e.preventDefault();
          e.stopPropagation();
          onDismiss();
          break;
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [commands, selectedIndex, onSelect, onDismiss]);

  if (!commands.length) return null;

  const style = {
    position: 'absolute',
    top: position.showAbove ? 'auto' : `${position.top}px`,
    bottom: position.showAbove ? `calc(100% - ${position.top}px)` : 'auto',
    left: `${position.left}px`,
  };

  return (
    <div class="command-autocomplete" ref={dropdownRef} style={style}>
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
