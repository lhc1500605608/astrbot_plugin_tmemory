/* avatar.js — Avatar 系统（hash-color + 首字母，Phase 3） */

const AVATAR_HUES = [211, 142, 36, 0, 270, 180, 320, 60];

function hashColor(str) {
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    hash = (hash * 31 + str.charCodeAt(i)) & 0xffffffff;
  }
  const hue = AVATAR_HUES[Math.abs(hash) % AVATAR_HUES.length];
  return `hsl(${hue}, 55%, 42%)`;
}

function makeAvatar(userId, extraClass) {
  const letter = userId ? userId.charAt(0).toUpperCase() : '?';
  const color = hashColor(userId || '');
  const el = document.createElement('div');
  el.className = 'avatar' + (extraClass ? ' ' + extraClass : '');
  el.textContent = letter;
  el.style.background = color;
  el.title = userId || '';
  return el;
}
