import { el, display } from './data';
import type { Parameter, Values } from './types';
export function renderField(p: Parameter, values: Values, onChange: () => void, allowed: () => boolean): {
    node: HTMLElement;
    sync: () => void;
} {
    const node = el('fieldset', 'rich-reply__field');
    const legend = el('legend', '', p.label + (p.required ? ' *' : '') + (p.unit ? `（${p.unit}）` : ''));
    node.append(legend);
    const output = el('output');
    const inputs: (HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement)[] = [];
    const emit = (): void => { if (allowed())
        onChange(); };
    if (p.type === 'radio' || p.type === 'multiselect') {
        for (const option of p.options ?? []) {
            const label = el('label', 'rich-reply__choice'), input = el('input');
            input.type = p.type === 'radio' ? 'radio' : 'checkbox';
            input.name = p.id;
            input.value = option;
            input.setAttribute('aria-label', `${p.label}：${option}`);
            input.addEventListener('change', () => {
                if (!allowed())
                    return;
                values[p.id] = p.type === 'radio' ? option : inputs.filter(i => (i as HTMLInputElement).checked).map(i => i.value);
                emit();
            });
            label.append(input, document.createTextNode(option));
            node.append(label);
            inputs.push(input);
        }
    }
    else {
        const input = p.type === 'select' ? el('select') : p.type === 'textarea' ? el('textarea') : el('input');
        input.name = p.id;
        input.setAttribute('aria-label', p.label);
        input.required = !!p.required;
        if (input instanceof HTMLSelectElement) {
            input.append(el('option', '', '请选择'));
            input.options[0].value = '';
            for (const option of p.options ?? []) {
                const o = el('option', '', option);
                o.value = option;
                input.append(o);
            }
        }
        else if (input instanceof HTMLInputElement) {
            input.type = ['range', 'number', 'date', 'checkbox'].includes(p.type) ? p.type : 'text';
            if (p.type === 'range' || p.type === 'number') {
                input.min = String(p.min);
                input.max = String(p.max);
                input.step = String(p.step);
            }
            else
                input.maxLength = 1000;
        }
        else {
            input.maxLength = 1000;
            input.rows = 3;
        }
        input.addEventListener('input', () => {
            if (!allowed())
                return;
            values[p.id] = p.type === 'checkbox' ? (input as HTMLInputElement).checked : p.type === 'range' || p.type === 'number' ? (input.value === '' ? '' : Number(input.value)) : input.value;
            output.textContent = display(values[p.id]);
            emit();
        });
        node.append(input);
        inputs.push(input);
        if (p.type === 'range')
            node.append(output);
    }
    const sync = (): void => {
        for (const input of inputs) {
            if (input instanceof HTMLInputElement && ['radio', 'checkbox'].includes(input.type))
                input.checked = p.type === 'checkbox' ? values[p.id] === true : p.type === 'radio' ? values[p.id] === input.value : (values[p.id] as string[]).includes(input.value);
            else
                input.value = String(values[p.id] ?? '');
        }
        output.textContent = display(values[p.id]);
    };
    sync();
    return { node, sync };
}
