'use strict';

function canonicalMonitorInput(text) {
  const lines = text.split(/\r?\n/).map(x => x.trim()).filter(Boolean);
  if (lines.length > 20) throw new Error('Maximum 20 products per group');
  return lines.map(line => {
    // Keep saved legacy price/offer limits when normalizing their product ID.
    const [raw, ...limits] = line.split(';');
    let asin = raw.trim().toUpperCase();
    if (/^https?:\/\//i.test(raw)) {
      const url = new URL(raw);
      if (url.username || url.password || !/^(www\.)?amazon\.(com|co\.uk|ca)$/.test(url.hostname)) {
        throw new Error('Use an Amazon US, UK, or Canada product link');
      }
      asin = url.pathname.match(/\/(?:dp|gp\/product|gp\/aw\/d)\/([a-z0-9]{10})(?:\/|$)/i)?.[1]?.toUpperCase() || '';
    }
    if (!/^[A-Z0-9]{10}$/.test(asin)) throw new Error('Enter a 10-character ASIN or Amazon product link');
    return asin + (limits.length ? ';' + limits.join(';') : '');
  }).join('\n');
}

function monitorAsins(group) {
  if (!group) return [];
  try { return [...new Set(canonicalMonitorInput(group.input_list_id?state.input_lists.find(x=>x.id===group.input_list_id)?.products||'':group.products||'').split('\n').filter(Boolean).map(x => x.split(';')[0]))]; }
  catch { return []; }
}

function monitorSelect(group, value='') {
  const asins = monitorAsins(group);
  const choices = [['', asins.length === 1 ? asins[0] : 'Any group monitor'], ...asins.map(x => [x, x])];
  if (value && !asins.includes(value)) choices.push([value, value + ' (removed — choose another)']);
  return select('monitor_asin', 'Assigned monitor', choices, value);
}

function taskMonitorSelect(group, task) {
  const asins = monitorAsins(group);
  if (asins.length < 2 && !task.monitor_asin) return `<span class="mono">${esc(asins[0] || 'No monitor input')}</span>`;
  return `<select aria-label="Assigned monitor for task" data-task-monitor="${esc(task.id)}" ${state.active.includes(task.id) ? 'disabled' : ''}>
    <option value="">Any group monitor</option>
    ${[...new Set([...asins, ...(task.monitor_asin ? [task.monitor_asin] : [])])].map(asin => `<option value="${esc(asin)}" ${task.monitor_asin===asin ? 'selected' : ''}>${esc(asin)}${asins.includes(asin) ? '' : ' (removed)'}</option>`).join('')}
  </select>`;
}

function monitorStatuses(group) {
  return monitorAsins(group).map(asin => {
    const records = (state.monitors || []).filter(m => m.group_id===group.id && m.asin===asin);
    const active = records.filter(m => m.task_ids.length);
    const visible = active.length ? active : records.slice(-1);
    return `<div class="monitor-status-row"><strong class="mono">${esc(asin)}</strong>${visible.length ? visible.map(m =>
      `<button type="button" data-monitor-log="${esc(m.id)}" title="Open monitor activity log">${badge(m.status)}<small>${m.simulation ? 'Simulation' : esc(m.region)} · ${esc(m.message)}</small></button>`
    ).join('') : '<small>Standby · start an assigned task</small>'}</div>`;
  }).join('') || '<p class="help">Add an ASIN to configure a monitor.</p>';
}

function refreshMonitorStatus() {
  const group = state.groups.find(g => g.id===groupId);
  const box = document.querySelector('#monitor-statuses');
  if (box && group) updateRegion(box, monitorStatuses(group));
  document.querySelectorAll('[data-monitor-log-body]').forEach(body => {
    const record = (state.monitors || []).find(m => m.id===body.dataset.monitorLogBody);
    if (record) updateRegion(body, `<p>${esc(record.connection)} · ${record.simulation ? 'Simulation' : esc(record.region)}</p>` +
      record.log.slice().reverse().map(entry => `<p><time>${esc(time(entry.at))}</time> · <strong>${esc(entry.status)}</strong><br>${esc(entry.message)}</p>`).join(''));
  });
}

document.addEventListener('focusout', event => {
  if (event.target.id !== 'monitor-products') return;
  const group = state.groups.find(g => g.id===groupId);
  if (group?.retailer !== 'amazon') return;
  try {
    event.target.value = canonicalMonitorInput(event.target.value);
    event.target.setCustomValidity('');
  } catch (error) {
    event.target.setCustomValidity(error.message);
    event.target.reportValidity();
  }
});
document.addEventListener('input', event => {
  if (event.target.id==='monitor-products') event.target.setCustomValidity('');
});
document.addEventListener('change', async event => {
  const id = event.target.dataset.taskMonitor;
  if (!id) return;
  const field = event.target;
  field.disabled = true;
  try { await api('tasks/' + id, 'PUT', {monitor_asin: field.value}); await refresh(); }
  catch (error) { toast(error.message); field.value = state.tasks.find(t => t.id===id)?.monitor_asin || ''; }
  finally { field.disabled = false; field.blur(); }
});
function performancePanel() {
  return '<details data-performance><summary>Step timings</summary><p class="help">Recent timings for this application session. Parent steps include their substeps; do not add the rows together. Waiting and polling pauses are intentional. Stock signal age includes time spent preparing the account.</p><button type="button" data-performance-refresh>Refresh timings</button><div data-performance-body aria-live="polite"></div></details>';
}

function bindPerformance(modal, kind, id) {
  const panel = modal.querySelector('[data-performance]');
  const button = panel.querySelector('[data-performance-refresh]');
  const body = panel.querySelector('[data-performance-body]');
  let loaded = false;
  const duration = ms => `${(ms / 1000).toFixed(3)} s`;
  async function update() {
    if (button.disabled) return;
    button.disabled = true;
    if (!loaded) body.textContent = 'Loading timings…';
    try {
      const data = await api(`performance?kind=${encodeURIComponent(kind)}&id=${encodeURIComponent(id)}`);
      if (!modal.isConnected) return;
      const active = data.active.map(row => `<li>${esc(row.stage)}: ${duration(row.duration_ms)} elapsed</li>`).join('');
      body.innerHTML = (active ? `<p>In progress</p><ul>${active}</ul>` : '') + (data.summary.length ?
        `<div class="table-wrap"><table><thead><tr><th>Step</th><th>Latest</th><th>Average</th><th>Longest</th><th>Count</th></tr></thead><tbody>${data.summary.map(row => `<tr><td>${esc(row.stage)}${row.errors ? `<small>${row.errors} failed</small>` : ''}</td><td>${duration(row.last_ms)}</td><td>${duration(row.mean_ms)}</td><td>${duration(row.max_ms)}</td><td>${row.count}</td></tr>`).join('')}</tbody></table></div>` : '<p class="help">No completed timings yet. Start the task, then refresh here. Timings reset when the application restarts.</p>');
      loaded = true;
    } catch (error) { body.textContent = error.message; }
    finally { button.disabled = false; }
  }
  panel.addEventListener('toggle', () => { if (panel.open && !loaded) update(); });
  button.onclick = update;
}

document.addEventListener('click', event => {
  const button = event.target.closest('[data-monitor-log]');
  if (!button) return;
  const record = (state.monitors || []).find(m => m.id===button.dataset.monitorLog);
  if (!record) return;
  const modal = dialog(`<h2>${esc(record.asin)} monitor activity</h2><p class="help">Latest 100 status changes for this monitor run.</p><div class="monitor-log" data-monitor-log-body="${esc(record.id)}"></div>${performancePanel()}<button type="button" data-close-monitor-log>Close</button>`);
  bindPerformance(modal, 'monitor', record.id);
  modal.querySelector('[data-close-monitor-log]').onclick = () => modal.close();
  refreshMonitorStatus();
});
