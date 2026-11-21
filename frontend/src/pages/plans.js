import {plot} from '../components/common/chart.js';
import {datasetField, feedback, field} from '../components/common/fields.js';
import {empty, metrics, panel, pill} from '../components/common/panels.js';
import {table} from '../components/common/table.js';
import {CONCLUSION, LOOK_STATUS, PLAN_STATUS} from '../constants/enums.js';
import {handler, poll} from '../hooks/action.js';
import {session} from '../stores/session.js';
import {workspace} from '../stores/workspace.js';
import {button, el, h, link, p} from '../utils/dom.js';
import {date, value} from '../utils/format.js';

const finished = item => item && item.status !== 'pending' && item.status !== 'running';

function alphaText(alphas) {
  return alphas.map(item => `${item}`).join(', ');
}

function planForm(spec, datasets, refresh, message) {
  const submit = button('锁定验收计划', {type:'submit', class:'primary', 'data-testid':'create-plan'});
  const alphas = el('label', {for:'plan-alphas', class:'field'}, el('span', {}, '各次显著性额度（可选）'),
    el('input', {id:'plan-alphas', name:'alphas', type:'text', placeholder:'留空则把总上限均分给各扫描批次'}),
    el('small', {}, '按批次顺序用逗号分隔；各次额度之和不得超过总上限。首查开始后不可更改。'));
  return el('form', {class:'fields', 'data-testid':'plan-form', onSubmit:handler(submit, message, async event => {
    event.preventDefault();
    const data = new FormData(event.target);
    const raw = (data.get('alphas') || '').trim();
    const parsed = raw ? raw.split(/[,，\s]+/).filter(Boolean).map(Number) : null;
    if (parsed && parsed.some(item => !Number.isFinite(item) || item <= 0)) {
      throw new Error('各次显著性额度须为大于 0 的数值');
    }
    const item = await workspace.createPlan({
      dataset_id: Number(data.get('dataset_id')),
      name: data.get('name'),
      params: Object.fromEntries(spec.plan_params.map(param => [param.key, data.get(param.key)])),
      alphas: parsed,
    });
    location.hash = `#plans/${item.id}`;
  })}, datasetField(datasets),
    field({key:'name', label:'计划名称', type:'string', max_length:120}, `批次验收计划`),
    el('div', {class:'form-grid'}, spec.plan_params.map(param => field(param))), alphas, submit);
}

function lookResult(look, spec) {
  const message = feedback();
  const download = button('下载本次 CSV', {class:'quiet', onClick:event => handler(download, message, () =>
    workspace.download(`/plans/${look.plan_id}/looks/${look.id}/export`,
      `${spec.short_name}-plan${look.plan_id}-look${look.sequence}.csv`))(event)});
  const result = look.result;
  if (!result) return p(look.error || '暂无结果', 'feedback');
  return el('section', {class:'analysis', 'data-testid':'look-complete'},
    el('div', {class:'result-head'}, p(`第 ${look.sequence} 次查看 · ${look.batch_id}`, 'eyeline'),
      h(2, result.headline), p(result.note, 'intro-copy'),
      el('div', {class:'actions'}, pill(LOOK_STATUS[look.status], look.status),
        pill(CONCLUSION[look.conclusion], look.conclusion), download, message)),
    metrics(result.metrics), result.charts.map(plot),
    (result.sections || []).map(item => panel(item.title, p(item.text))),
    result.tables.map(item => panel(item.title, table(item.columns, item.rows, {caption:item.title}))));
}

export async function plansPage(route, refresh) {
  const {spec, datasets} = workspace;
  const plans = await workspace.plans();
  const message = feedback();
  const id = route.id || plans[0]?.id;
  const plan = id ? await workspace.getPlan(id) : null;
  if (plan && plan.current && !finished(plan.current)) {
    poll(() => workspace.getPlan(plan.id), item => finished(item.current), () => location.hash.startsWith(`#plans/${plan.id}`))
      .then(result => { if (result) refresh(); })
      .catch(error => { message.textContent = error.message; });
  }
  const canSee = session.canWrite;

  const planList = table([
    {label:'计划', render:row => link(row.name, `#plans/${row.id}`)},
    {label:'锁定批次', render:row => `${row.batches.length} 个`},
    {label:'已用 / 总额度', render:row => `${value(row.used_alpha)} / ${value(row.alpha_total)}`},
    {label:'净增门槛', render:row => `${value(row.min_gain_pp)} 个百分点`},
    {label:'状态', render:row => pill(PLAN_STATUS[row.status], row.status)},
    {label:'创建', render:row => date(row.created_at)},
  ], plans, {caption:'验收计划', empty:'先在下方锁定一个分批验收计划。'});

  if (!plan) {
    return el('div', {class:'two-col'},
      el('div', {class:'wide-panel'},
        canSee ? panel(spec.pages[6], p('首查前锁定批次顺序、各次显著性额度和净增门槛。', 'subtle'),
          datasets.length ? planForm(spec, datasets, refresh, message) : empty('请先导入数据。', '#inputs', '前往导入'),
          message) : panel(spec.pages[6], p('等待分析员锁定验收计划。', 'empty')),
        panel('验收计划', planList)),
      el('aside', {class:'sidebar-panel'}, panel('计算口径', el('ul', {class:'method-list'}, spec.methodology.map(text => el('li', {}, text))))));
  }

  const current = plan.current;
  const next = plan.next_sequence;
  const retry = current && current.status === 'failed' && !current.conclusion;
  const blocked = plan.status === 'sealed' ? '该计划已封存结论。'
    : (!next ? '所有批次已查看完毕。'
      : (current && (current.status === 'pending' || current.status === 'running'))
        ? `第 ${current.sequence} 次查看正在进行，同一批次只占一次额度。`
        : (current && current.conclusion !== 'pass' && !current.published_at && !retry)
          ? `请先发布第 ${current.sequence} 次未达标结果，才能查看下一批。` : null);
  const start = canSee && next && !blocked && button(
    retry ? `重跑第 ${next} 批（沿用锁定额度）` : `查看第 ${next} 批（${plan.next_batch}）`,
    {class:'primary', 'data-testid':'start-look'});
  const publish = session.canReview && current && finished(current) && current.conclusion !== 'pass' && !current.published_at
    && button(`发布第 ${current.sequence} 次结果`, {class:'primary', 'data-testid':'publish-look'});
  if (start) start.addEventListener('click', handler(start, message, async () => {
    const item = await workspace.startLook(plan.id);
    message.textContent = item.created ? '已占用本批额度，后台开始判定。'
      : (item.look.status === 'pending' ? '沿用本批次已占用的额度和样本重跑。' : '本批次已占用额度，沿用本次查看。');
    await refresh();
  }));
  if (publish) publish.addEventListener('click', handler(publish, message, async () => {
    await workspace.publishLook(plan.id, current.id);
    await refresh();
  }));

  const ledger = table([
    {key:'sequence', label:'次序'},
    {key:'batch_id', label:'扫描批次'},
    {key:'alpha', label:'显著性额度'},
    {key:'min_gain_pp', label:'净增门槛'},
    {label:'累计配对', render:row => value(row.pairs)},
    {label:'净增', render:row => value(row.gain_pp)},
    {label:'p 值', render:row => value(row.p_value)},
    {label:'结论', render:row => row.conclusion ? pill(CONCLUSION[row.conclusion], row.conclusion) : '—'},
    {label:'发布', render:row => row.published_at ? `${date(row.published_at)} · ${row.published_by}` : '未发布'},
    {label:'状态', render:row => pill(LOOK_STATUS[row.status], row.status)},
    {label:'导出', render:row => {
      if (!row.has_export) return '—';
      const download = button('下载', {class:'quiet'});
      download.addEventListener('click', handler(download, message, () =>
        workspace.download(`/plans/${plan.id}/looks/${row.id}/export`,
          `${spec.short_name}-plan${plan.id}-look${row.sequence}.csv`)));
      return download;
    }},
  ], plan.looks, {caption:'分批查看台账', empty:'尚未发起任何查看。', pageSize:6});

  const summary = metrics([
    {label:'批次顺序', value:plan.batches.join(' → ')},
    {label:'各次额度', value:alphaText(plan.alphas)},
    {label:'已用 / 总额度', value:`${value(plan.used_alpha)} / ${value(plan.alpha_total)}`},
    {label:'净增门槛', value:`${value(plan.min_gain_pp)} 个百分点`},
    {label:'最少配对数', value:plan.min_pairs},
    {label:'状态', value:PLAN_STATUS[plan.status]},
  ]);

  return el('div', {class:'two-col'},
    el('div', {class:'wide-panel'},
      panel(plan.name, p('批次顺序、各次额度和净增门槛已在首查前锁定，后续不可更改。', 'subtle'),
        summary,
        el('div', {class:'actions'}, pill(PLAN_STATUS[plan.status], plan.status), start, publish, message),
        blocked && p(blocked, 'subtle')),
      current && finished(current) && lookResult(current, spec),
      panel('分批查看台账', p('核对每次用过的批次、额度和结论；达标后结论封存，未达标结果须按顺序发布。', 'subtle'), ledger)),
    el('aside', {class:'sidebar-panel'},
      panel('验收计划', planList),
      panel('数据来源', link(`批次 #${plan.dataset_id}`, `#inputs/${plan.dataset_id}`, {class:'quiet'}),
        p(`指纹 ${plan.dataset_checksum.slice(0, 16)}…`, 'subtle'),
        p(`发起人 ${plan.author}`, 'subtle')),
      panel('计算口径', el('ul', {class:'method-list'}, spec.methodology.map(text => el('li', {}, text))))));
}
