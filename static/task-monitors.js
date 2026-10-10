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
  try { return [...new Set(canonicalMonitorInput(groupProducts(group)).split('\n').filter(Boolean).map(x => x.split(';')[0]))]; }
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
  if (box && group) box.innerHTML = monitorStatuses(group);
  document.querySelectorAll('[data-monitor-log-body]').forEach(body => {
    const record = (state.monitors || []).find(m => m.id===body.dataset.monitorLogBody);
    if (record) body.innerHTML = `<p>${esc(record.connection)} · ${record.simulation ? 'Simulation' : esc(record.region)}</p>` +
      record.log.slice().reverse().map(entry => `<p><time>${esc(time(entry.at))}</time> · <strong>${esc(entry.status)}</strong><br>${esc(entry.message)}</p>`).join('');
  });
}

async function saveMonitorDraft() {
  const form = document.querySelector('#group-settings');
  if (!form) return;
  const group = state.groups.find(g => g.id===form.dataset.id);
  if (!group) return;
  const data = formData(form);
  if (group.retailer==='amazon') data.products = canonicalMonitorInput(data.products);
  form.elements.products.value = data.products;
  for (const key of ['delay_ms','max_total','max_errors','min_discount','min_savings','min_price','retry_delay_ms','quantity','max_checkouts']) {
    if (key in data) data[key] = Number(data[key]);
  }
  data.max_price = data.max_price==='' ? null : Number(data.max_price);
  const saved = await api('groups/' + group.id, 'PUT', data);
  Object.assign(group, saved);
  delete form.dataset.dirty;
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
document.addEventListener('click', event => {
  const button = event.target.closest('[data-monitor-log]');
  if (!button) return;
  const record = (state.monitors || []).find(m => m.id===button.dataset.monitorLog);
  if (!record) return;
  const modal = dialog(`<h2>${esc(record.asin)} monitor activity</h2><p class="help">Latest 100 status changes for this monitor run.</p><div class="monitor-log" data-monitor-log-body="${esc(record.id)}"></div><button type="button" data-close-monitor-log>Close</button>`);
  modal.querySelector('[data-close-monitor-log]').onclick = () => modal.close();
  refreshMonitorStatus();
});
