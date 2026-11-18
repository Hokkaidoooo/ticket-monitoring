const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const pagePath = path.join(__dirname, '..', 'webui_assets', 'page.html');
const html = fs.readFileSync(pagePath, 'utf8');
const scripts = Array.from(html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g));
scripts.forEach((match, index) => new vm.Script(match[1], { filename: 'page-inline-' + (index + 1) + '.js' }));
const component = html.match(/<script id="final-price-code">([\s\S]*?)<\/script>/);
assert.ok(component, '费用核对脚本必须在实际页面中加载');
const escapeFunction = html.match(/function he\(s\)\{[^\n]+\}/);
assert.ok(escapeFunction, '页面必须提供统一HTML转义');
const elements = new Map();
let chargeRows = [];
const monitorTabs = ['overview', 'specials', 'trend', 'watch', 'details', 'health'].map(t => {
  const element = makeElement();
  element.dataset = { t };
  return element;
});
function makeElement() {
  let content = '';
  return {
    addEventListener() {}, checked: false, value: '', readOnly: false, disabled: false,
    style: { display: '' }, closest() { return this; },
    get textContent() { return content; },
    set textContent(value) { content = String(value); },
    get innerHTML() { return content; },
    set innerHTML(value) { content = String(value); },
    insertAdjacentHTML(position, value) {
      assert.equal(position, 'beforeend');
      content += value;
    },
    showModal() { this.open = true; },
    close() { this.open = false; }
  };
}
const context = vm.createContext({
  console,
  document: { getElementById: id => {
    if (!elements.has(id)) elements.set(id, makeElement());
    return elements.get(id);
  }, querySelector: () => null, querySelectorAll: selector => selector === '#montabs .mtab' ? monitorTabs : chargeRows }
});
vm.runInContext(escapeFunction[0] + '\n' + component[1], context);
let checks = 0;
function check(condition, message) {
  assert.ok(condition, message);
  checks++;
}
context.$ = id => context.document.getElementById(id);
for (const name of ['monTabNeedsUser', 'syncMonTabAvailability', 'srxRadarOwnerName']) {
  const source = html.match(new RegExp('^function ' + name + '\\([^]*?^\\}', 'm'));
  assert.ok(source, name + '必须来自实际页面');
  vm.runInContext(source[0], context);
}
check(!context.monTabNeedsUser('specials'), '手动查价无需先配置用户');
check(context.monTabNeedsUser('details'), '监控明细需要用户数据');
context.syncMonTabAvailability(false);
check(monitorTabs.filter(tab => tab.style.display === '').map(tab => tab.dataset.t).join(',') === 'overview,specials', '首次使用只显示概览与可用的手动查价');
check(elements.get('spRadarMonitor').style.display === 'none' && elements.get('spfullcard').style.display === 'none', '没有用户时不显示等待监控记录的面板');
check(elements.get('spRadarAlertSave').style.display === 'none', '没有用户时不提供保存监控提醒');
context.syncMonTabAvailability(true);
check(monitorTabs.every(tab => tab.style.display === ''), '配置用户后恢复全部监控视图');
context.S = { users: [] }; context.U = 0;
check(context.srxRadarOwnerName() === null, '手动查询不能创建假用户');
context.S = { users: [{ name: '测试用户' }] };
check(context.srxRadarOwnerName() === '测试用户', '用户查询保留真实所属用户');
const incomplete = {
  price: 450,
  quote: {
    quoted_amount: 450, basis: 'bare', payable_total: null, complete: false,
    source_type: 'list', charges: [], missing: ['税费', '保险和服务费'], extras_complete: false, discounts_complete: false, confirmed: false
  }
};
const quoted = context.finalPriceHtml(incomplete, { interactive: false });
check(quoted.includes('最终价未确认'), '裸价必须明确最终价未确认');
check(quoted.includes('来源报价 ¥450'), '应保留来源报价');
check(quoted.includes('税费、保险和服务费'), '应显示尚未确认的费用');
check(!quoted.includes('最终价 ¥450'), '不能把裸价冒充最终价');
const taxed = {
  price: 520,
  quote: Object.assign({}, incomplete.quote, { quoted_amount: 520, basis: 'tax_included', missing: ['保险和服务费'] })
};
check(context.finalPriceHtml(taxed, { interactive: false }).includes('最终价未确认'), '含税报价仍需确认附加费用');
check(context.finalPriceHtml(taxed, { interactive: false }).includes('含税报价'), '应区分含税报价口径');
const empty = context.finalPriceHtml({}, { interactive: false });
check(empty.includes('来源报价未提供') && empty.includes('最终价未确认'), '没有默认报价时应保留空值状态');
check(!empty.includes('¥0'), '空报价不能默认成0');
const includedTax = context.fqBreakdownHtml({ charges: [{ id: 'tax', kind: 'tax', label: '税费', amount: null, included: true, selected: true, mandatory: true }] });
check(includedTax.includes('金额未披露') && includedTax.includes('已含，不重复计入'), '已含税额未拆分时不能再加税或显示零税');
const completed = {
  price: 450,
  qual: true,
  quote: {
    quoted_amount: 450, basis: 'bare', payable_total: 530.5, complete: true,
    source_type: 'manual_checkout', charges: [], missing: [], extras_complete: true, discounts_complete: true, confirmed: true
  }
};
const confirmed = context.finalPriceHtml(completed, { interactive: false });
check(confirmed.includes('最终价 ¥530.5'), '应显示后台返回的最终价');
check(confirmed.includes('手工核对'), '手工结果必须注明来源');
check(!confirmed.includes('渠道已确认全部费用'), '手工结果不能冒充渠道自动验证');
check(confirmed.includes('本单当前票价 ¥450'), '手工核对应明确本单当前票价');
check(context.finalPriceValue(incomplete) === null, '未确认金额不能参与最终价范围比较');
check(context.finalPriceValue(completed) === 530.5, '价格比较应使用最终价');
check(context.finalPriceCompare(incomplete, completed) > 0, '未确认价应排在已确认价之后');
check(!context.finalPriceQualifies(Object.assign({}, incomplete, { qual: true })), '报价达标不能冒充最终价达标');
check(context.finalTaxLabel({ price: 450, intl: true }).includes('税费未确认'), '国际属性不能证明含税');
const charge = { id: 'insurance', kind: 'insurance', label: '绑定保险', amount: null, included: null, selected: true, mandatory: false };
const editor = context.fqChargeHtml(charge, false);
check(editor.includes('data-fq-amount min="0" step="0.01" value=""'), '未知费用金额不能默认为0');
check(editor.includes('value="unknown" selected'), '未知是否已含不能默认为另收费或已含');
const summary = context.fqBreakdownHtml({ charges: [Object.assign({}, charge, { amount: 20, included: false })] });
check(summary.includes('当前已选，可取消') && summary.includes('另收，计入最终价'), '应注明选中的可取消费用');
const excluded = context.fqBreakdownHtml({ charges: [Object.assign({}, charge, { amount: 20, included: false, selected: false })] });
check(excluded.includes('未选，不计入'), '取消的可选费用不计入最终价');
const discount = {
  id: 'discount-1', kind: 'discount', label: '本单测试优惠', amount: 20,
  included: false, selected: true, mandatory: false, applicable: true, applied: true
};
const discountSummary = context.fqBreakdownHtml({ charges: [discount] });
check(discountSummary.includes('抵扣 ¥20') && discountSummary.includes('本单已生效，抵扣最终价'), '已生效本单优惠应显示实际抵扣');
check(!discountSummary.includes('另收') && !discountSummary.includes('不可取消'), '优惠不能显示为加收费用或必选费用');
const includedDiscount = context.fqBreakdownHtml({ charges: [Object.assign({}, discount, { included: true })] });
check(includedDiscount.includes('已从当前票价扣除，不再抵扣'), '已扣优惠不能重复抵扣');
const unappliedDiscount = context.fqBreakdownHtml({ charges: [Object.assign({}, discount, { applied: false })] });
check(unappliedDiscount.includes('本单尚未生效，不抵扣'), '仅账号可用未生效的优惠不能当作已抵扣');
const unknownDiscount = Object.assign({}, discount, { amount: null, included: null, applicable: null, applied: null });
const discountEditor = context.fqChargeHtml(unknownDiscount, true);
check(discountEditor.includes('本单单人实际抵扣（元）') && discountEditor.includes('value=""'), '优惠填写本单实际抵扣且未知金额留空');
for (const field of ['applicable', 'applied']) {
  const select = discountEditor.match(new RegExp('data-fq-' + field + '>([\\s\\S]*?)</select>'));
  check(select && select[1].includes('value="unknown" selected') && !select[1].includes('value="yes" selected'), '优惠' + field + '初始未知，不能默认生效');
}
check(!discountEditor.includes('data-fq-mandatory'), '优惠不可被设成必选收费');
check(context.fqBreakdownHtml({ charges: [unknownDiscount] }).includes('本单是否适用尚未确认'), '适用未知必须明确展示');
check(context.fqBreakdownHtml({ charges: [Object.assign({}, unknownDiscount, { applicable: true })] }).includes('本单是否生效尚未确认'), '生效未知必须明确展示');
check(context.fqBreakdownHtml({ charges: [Object.assign({}, discount, { included: true, selected: false })] }).includes('已取消，需重新核对当前票价'), '取消已扣优惠应要求重新核价');
const zero = Object.assign({}, completed, { quote: Object.assign({}, completed.quote, { quoted_amount: 0, payable_total: 0 }) });
check(context.finalPriceHtml(zero, { interactive: false }).includes('最终价 ¥0'), '合法最终价0应正常展示');
check(context.finalPriceValue(zero) === 0 && context.finalPriceCompare(zero, completed) < 0, '最终价0应参与金额比较');
const safe = context.fqOpenButton(Object.assign({}, incomplete, { route: '<img src=x onerror=alert(1)>' }));
check(!safe.includes('<img src=x'), '报价上下文必须转义');
vm.runInContext('FINAL_PRICE_ACTIVE={revision:0}', context);
context.document.getElementById('finalQuoteConfirmed').checked = true;
context.document.getElementById('finalQuoteResult').textContent = '最终价 ¥530.5';
context.fqInvalidateResult({ target: { id: 'changedFee' } });
check(!elements.get('finalQuoteConfirmed').checked, '费用改变后应重新核对全部费用');
check(elements.get('finalQuoteResult').textContent === '票价、费用或优惠已修改，请重新计算最终价。', '票价、费用或优惠改变后不能继续显示旧最终价');
check(vm.runInContext('FINAL_PRICE_ACTIVE.revision', context) === 1, '应识别计算期间发生的费用修改');

function openSource() {
  chargeRows = [];
  context.fqOpen({ dataset: { fqSeed: JSON.stringify(context.fqSeed(incomplete)) } });
}
function discountRow(values) {
  const controls = {
    '[data-fq-label]': { tagName: 'INPUT', value: discount.label },
    '[data-fq-amount]': { value: values.amount },
    '[data-fq-included]': { value: values.included },
    '[data-fq-selected]': { checked: values.selected },
    '[data-fq-applicable]': { value: values.applicable },
    '[data-fq-applied]': { value: values.applied }
  };
  return {
    dataset: { chargeId: discount.id, chargeKind: 'discount' },
    querySelector: selector => controls[selector]
  };
}
async function verifyInteractions() {
  openSource();
  check(elements.get('finalQuoteReference').textContent === '¥450 · 裸价不含税', '采集报价应保留为只读参考');
  check(elements.get('finalQuoteAmount').value === '' && !elements.get('finalQuoteAmount').readOnly, '当前票价不能从匿名来源报价自动填入');
  check(elements.get('finalQuoteBasis').value === 'unknown', '当前票价口径需独立核对');
  check(elements.get('finalQuoteResult').textContent === '', '尚未填当前票价时不能显示旧核对结果');
  check(!elements.get('finalQuoteCharges').innerHTML.includes('data-charge-kind="discount"'), '未核对优惠不能自动添加或默认无券');
  context.fqAddCharge('discount');
  check(elements.get('finalQuoteCharges').innerHTML.includes('data-charge-kind="discount"'), '应能主动添加本单优惠');
  assert.throws(() => context.fqAddCharge('invalid'), /费用项目类型无效/);
  checks++;
  context.fqOpen();
  check(elements.get('finalQuoteAmount').value === '' && elements.get('finalQuoteReference').textContent === '尚未提供', '全局核对入口应保持当前票价为空');

  openSource();
  chargeRows = [discountRow({ amount: '', included: 'unknown', selected: true, applicable: 'unknown', applied: 'unknown' })];
  const unknownRead = context.fqReadCharges()[0];
  check(unknownRead.amount === null && unknownRead.applicable === null && unknownRead.applied === null && unknownRead.included === null, '读取未知优惠应保留空值');
  check(unknownRead.mandatory === false, '提交优惠必须保持可选');
  assert.throws(() => context.fqReadBool('invalid'), /优惠核对状态无效/);
  checks++;

  chargeRows = [discountRow({ amount: '20', included: 'extra', selected: true, applicable: 'yes', applied: 'yes' })];
  const requests = [];
  const backendQuote = Object.assign({}, completed.quote, { quoted_amount: 470, payable_total: 450, charges: [discount] });
  context.fetch = async (url, options) => {
    requests.push({ url, payload: JSON.parse(options.body) });
    return { ok: true, json: async () => ({ ok: true, quote: backendQuote }) };
  };
  elements.get('finalQuoteAmount').value = '470';
  elements.get('finalQuoteBasis').value = 'tax_included';
  elements.get('finalQuoteConfirmed').checked = true;
  await context.fqSubmit({ preventDefault() {} });
  const request = requests[0];
  check(request.url === '/api/price-quote' && request.payload.quoted_amount === 470, '应提交独立的本单当前票价，而非匿名450');
  check(request.payload.extras_complete === true && request.payload.discounts_complete === true && request.payload.confirmed === true, '费用与优惠核对后应明确传递三个确认项');
  check(request.payload.charges[0].applicable === true && request.payload.charges[0].applied === true && request.payload.charges[0].amount === 20, '提交优惠应保留本单适用、生效及实际抵扣');
  check(elements.get('finalQuoteResult').innerHTML.includes('最终价 ¥450') && !elements.get('finalQuoteResult').innerHTML.includes('最终价 ¥430'), '前端应直接展示后端金额，不能再次扣优惠');

  chargeRows = [];
  elements.get('finalQuoteAmount').value = '0';
  context.fetch = async (url, options) => {
    requests.push({ url, payload: JSON.parse(options.body) });
    return { ok: true, json: async () => ({ ok: true, quote: zero.quote }) };
  };
  await context.fqSubmit({ preventDefault() {} });
  check(requests[1].payload.quoted_amount === 0 && elements.get('finalQuoteResult').innerHTML.includes('最终价 ¥0'), '当前票价0和后端最终价0不得回退到匿名报价');
  elements.get('finalQuoteAmount').value = '';
  elements.get('finalQuoteConfirmed').checked = false;
  context.fetch = async (url, options) => {
    requests.push({ url, payload: JSON.parse(options.body) });
    return { ok: true, json: async () => ({ ok: true, quote: Object.assign({}, incomplete.quote, { quoted_amount: null, source_type: 'manual_checkout' }) }) };
  };
  await context.fqSubmit({ preventDefault() {} });
  check(requests[2].payload.quoted_amount === null, '未填写当前票价时应传空值');
  check(requests[2].payload.discounts_complete === false && requests[2].payload.extras_complete === false && requests[2].payload.confirmed === false, '未勾核对时不能默认无优惠或已确认');

  openSource();
  elements.get('finalQuoteAmount').value = '470';
  let resolveResponse;
  context.fetch = () => new Promise(resolve => { resolveResponse = resolve; });
  const pending = context.fqSubmit({ preventDefault() {} });
  context.fqInvalidateResult({ target: { id: 'finalQuoteAmount' } });
  resolveResponse({ ok: true, json: async () => ({ ok: true, quote: backendQuote }) });
  await pending;
  check(elements.get('finalQuoteResult').textContent === '票价、费用或优惠已修改，请重新计算最终价。', '计算期间改价后旧响应不能覆盖核对状态');
  check(!elements.get('finalQuoteSubmit').disabled, '旧响应结束后应恢复计算按钮');

  console.log(JSON.stringify({ inlineScripts: scripts.length, syntax: 'passed', renderingChecks: checks, page: pagePath }));
}
verifyInteractions().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
