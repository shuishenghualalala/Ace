declare const userInput: string;
declare function escapeHtml(value: string): string;

const root = document.createElement('div');
const label = document.createElement('span');
label.textContent = userInput;
root.replaceChildren(label);
root.innerHTML = `<span>${escapeHtml(userInput)}</span>`;
