/* Read-only guided tour: only workspace navigation is automated. */
(() => {
    'use strict';
    const launch = document.getElementById('mlops-tour-start');
    if (!launch) return;
    const steps = [
        ['overview', 'summary-worker', 'Check service health', 'Refresh the console and confirm a healthy worker before starting. Unknown or unavailable status is not ready.'],
        ['prepare', 'compute-features-btn', '1. Collect features', 'Compute features from existing observations. Then follow the collection job in Overview. This tour never starts a job for you.'],
        ['prepare', 'dataset-name-input', '2. Build a reusable dataset', 'In the dataset form, enter a name, choose a compatible definition and inspect the date range and split. Build once, then wait for the job to finish.'],
        ['prepare', 'workflow-dataset', '3. Inspect and validate', 'Select your saved dataset here. Review Dataset and Validation before training. If this list is empty, complete a dataset build first.'],
        ['prepare', 'training-dataset-select', '4. Configure training', 'Select the dataset you inspected and a compatible model. For a first person-behavior run, use Isolation Forest, seed 42 and default parameters. Leave optional tuning off.'],
        ['overview', 'jobs-refresh-btn', '5. Follow the job', 'After submitting training, follow its stage and messages in Work in progress. A failed job needs its cause corrected before retrying.'],
        ['review', 'workflow-model', '6. Review the evidence', 'Select the resulting model, then open Evaluation and Run summary. Completion does not mean approval: check held-out results, lineage and applicable gates.'],
        ['review', 'notebook-launch', 'Debug with a notebook', 'Download a debug notebook from a dataset or its job diagnostics, then open it here. Your VAS login is reused. Run cells in order and inspect the first failure.'],
        ['monitor', 'mlops-workspace-title', '7. Monitor permitted use', 'Approval is a separate, reasoned action in Review models. After the intended observation path is active, inspect predictions and fallbacks here. Check capability status before requesting drift analysis.'],
        ['audit', 'mlops-workspace-title', '8. Trace and troubleshoot', 'Use Audit to match actions, errors and request IDs. You have completed the tour—not the operational jobs. Return to the recommended next step to begin your own run.']
    ];
    function element(tag, cls, text) {
        const node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text) node.textContent = text;
        return node;
    }
    const dialog = element('dialog', 'mlops-tour');
    dialog.setAttribute('aria-labelledby', 'mlops-tour-title');
    dialog.setAttribute('aria-describedby', 'mlops-tour-description');
    const spot = element('div', 'mlops-tour-spot'); spot.setAttribute('aria-hidden', 'true');
    const card = element('section', 'mlops-tour-card');
    const label = element('p', 'mlops-tour-count');
    const title = element('h2'); title.id = 'mlops-tour-title'; title.tabIndex = -1;
    const description = element('p'); description.id = 'mlops-tour-description';
    const note = element('p', 'mlops-tour-note', 'Tour preview · no jobs or approvals are submitted');
    const actions = element('div', 'mlops-tour-actions');
    function button(text, fn) { const b = element('button', 'mlops-btn', text); b.type = 'button'; b.addEventListener('click', fn); return b; }
    let index = 0, target = null, frame = 0, returnFocus = launch, evidenceWasOpen = false;
    const back = button('← Back', () => { index--; show(); });
    const next = button('Next →', () => { if (index === steps.length - 1) dialog.close(); else { index++; show(); } });
    next.classList.add('mlops-btn-primary');
    const go = button('Go to this control', () => { returnFocus = target; dialog.close(); });
    const close = button('Close tour', () => dialog.close());
    actions.append(back, next, go, close);
    card.append(label, title, description, note, actions); dialog.append(spot, card); document.body.append(dialog);
    function position() {
        if (!dialog.open || !target) return;
        const rect = target.getBoundingClientRect(), pad = 7, gap = 16;
        const w = window.innerWidth, h = window.innerHeight;
        spot.style.left = Math.max(4, rect.left - pad) + 'px';
        spot.style.top = Math.max(4, rect.top - pad) + 'px';
        spot.style.width = Math.max(0, Math.min(rect.width + pad * 2, w - Math.max(4, rect.left - pad) - 4)) + 'px';
        spot.style.height = Math.max(0, Math.min(rect.height + pad * 2, h - Math.max(4, rect.top - pad) - 4)) + 'px';
        const cw = card.offsetWidth, ch = card.offsetHeight;
        const left = Math.max(12, Math.min(rect.left, w - cw - 12));
        const below = rect.bottom + gap + ch <= h - 12;
        const top = below ? rect.bottom + gap : Math.max(12, rect.top - gap - ch);
        card.style.left = left + 'px'; card.style.top = Math.min(top, Math.max(12, h - ch - 12)) + 'px';
        card.dataset.arrow = below ? 'up' : 'down';
        card.style.setProperty('--tour-arrow-left', Math.max(24, Math.min(cw - 24, rect.left + rect.width / 2 - left)) + 'px');
    }
    function schedulePosition() { cancelAnimationFrame(frame); frame = requestAnimationFrame(position); }
    function show() {
        const [workspace, id, heading, copy] = steps[index];
        document.querySelector('[data-mlops-view="' + workspace + '"]').click();
        const evidence = document.getElementById('mlops-evidence-browser');
        evidence.open = id === 'workflow-dataset' || id === 'workflow-model';
        target = document.getElementById(id);
        label.textContent = 'GUIDED WALKTHROUGH · ' + (index + 1) + ' OF ' + steps.length;
        title.textContent = heading; description.textContent = copy;
        back.disabled = index === 0; next.textContent = index === steps.length - 1 ? 'Finish tour' : 'Next →';
        go.disabled = !target || target.disabled || !target.getClientRects().length;
        if (target) target.scrollIntoView({block: 'center', behavior: 'instant'});
        title.focus({preventScroll: true}); position(); schedulePosition();
    }
    launch.addEventListener('click', () => {
        index = 0; returnFocus = launch;
        evidenceWasOpen = document.getElementById('mlops-evidence-browser').open;
        dialog.showModal(); show();
        window.addEventListener('resize', schedulePosition);
        window.addEventListener('scroll', schedulePosition, true);
    });
    dialog.addEventListener('close', () => {
        cancelAnimationFrame(frame);
        window.removeEventListener('resize', schedulePosition);
        window.removeEventListener('scroll', schedulePosition, true);
        if (returnFocus === launch) document.getElementById('mlops-evidence-browser').open = evidenceWasOpen;
        const control = returnFocus && (returnFocus.matches('button,select,input,a') ? returnFocus : returnFocus.querySelector('button,select,input,a'));
        const focus = control || returnFocus || launch;
        if (!focus.matches('button,select,input,a,[tabindex]')) focus.tabIndex = -1;
        focus.focus({preventScroll: false});
    });
})();
