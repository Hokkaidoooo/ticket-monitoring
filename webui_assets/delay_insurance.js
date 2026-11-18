'use strict';

let DELAY_INSURANCE_ACTIVE = null;
let DELAY_INSURANCE_INITIALISED = false;
let DELAY_INSURANCE_AIRPORTS_LOADED = false;
const DELAY_INSURANCE_REQUEST_TIMEOUT_MS = 90000;

function diElement(id) { return document.getElementById(id); }

function diSeed(record) {
 const source = record || {};
 const number = String(source.code || source.flight_no || source.flightNo || '').trim().toUpperCase();
 if (source.cal || source.transfer || !/^([A-Z0-9]{2}|[A-Z]{3})\d{1,5}[A-Z]?$/.test(number)) return null;
 return {
  flight_no: number,
  flight_date: source.date || source.depDate || source.depart_date || '',
  departure_airport: source.depAirportCode || source.departure_airport || '',
  arrival_airport: source.arrAirportCode || source.arrival_airport || ''
 };
}

function diOpenButton(record) {
 const seed = diSeed(record);
 if (!seed) return '';
 return '<button type="button" class="fq-open" data-di-seed="' + he(JSON.stringify(seed)) + '" onclick="event.stopPropagation();diOpen(this)" onkeydown="event.stopPropagation()">评估延误险</button>';
}

function diTomorrow() {
 const current = new Date(Date.now() + 24 * 60 * 60 * 1000);
 return new Intl.DateTimeFormat('en-CA', {timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit'}).format(current);
}

function diStopRequest(active) {
 if (!active) return;
 if (active.controller) active.controller.abort();
 clearInterval(active.progressTimer);
 clearTimeout(active.timeoutTimer);
}

function diOpen(button) {
 diStopRequest(DELAY_INSURANCE_ACTIVE);
 DELAY_INSURANCE_ACTIVE = {revision: 0};
 if (!DELAY_INSURANCE_INITIALISED) {
  diElement('diFlightDate').value = diTomorrow();
  DELAY_INSURANCE_INITIALISED = true;
 }
 if (button) {
  const seed = JSON.parse(button.dataset.diSeed);
  diElement('diFlightNo').value = seed.flight_no;
  if (seed.flight_date) diElement('diFlightDate').value = seed.flight_date;
  diElement('diDepartureAirport').value = seed.departure_airport;
  diElement('diArrivalAirport').value = seed.arrival_airport;
 }
 diElement('delayInsuranceError').textContent = '';
 diElement('delayInsuranceProgress').textContent = '';
 diElement('delayInsuranceResult').textContent = '';
 diElement('delayInsuranceSubmit').disabled = false;
 diElement('delayInsuranceDialog').showModal();
 diLoadAirports();
}

function diClose() { diElement('delayInsuranceDialog').close(); }

function diInvalidate() {
 const active = DELAY_INSURANCE_ACTIVE;
 if (!active) return;
 active.revision += 1;
 if (diElement('delayInsuranceResult').textContent) {
  diElement('delayInsuranceResult').textContent = '条件已修改，请重新评估。';
 }
 diElement('delayInsuranceError').textContent = '';
}

async function diLoadAirports() {
 if (DELAY_INSURANCE_AIRPORTS_LOADED) return;
 try {
  const response = await fetch('/api/delay-insurance/airports', {cache: 'no-store'});
  if (!response.ok) throw new Error('机场列表查询失败：HTTP ' + response.status);
  const result = await response.json();
  if (!Array.isArray(result.airports)) throw new Error('机场列表响应格式无效');
  diElement('diAirports').innerHTML = result.airports.map(airport => '<option value="' + he(airport.code) + '">' + he(airport.name) + '</option>').join('');
  DELAY_INSURANCE_AIRPORTS_LOADED = true;
 } catch (error) {
  console.error('延误险机场列表查询失败', error);
  diElement('delayInsuranceError').textContent = error.message;
 }
}

function diOptionalMoney(id) {
 const value = diElement(id).value;
 if (value === '') return null;
 const amount = Number(value);
 if (!Number.isFinite(amount)) throw new Error('请填写有效的保险金额');
 return amount;
}

function diPayload() {
 return {
  flight_no: diElement('diFlightNo').value.trim().toUpperCase(),
  flight_date: diElement('diFlightDate').value,
  departure_airport: diElement('diDepartureAirport').value.trim().toUpperCase(),
  arrival_airport: diElement('diArrivalAirport').value.trim().toUpperCase(),
  premium: diOptionalMoney('diPremium'), payout: diOptionalMoney('diPayout'),
  threshold_minutes: Number(diElement('diThreshold').value),
  claim_basis: diElement('diClaimBasis').value, policy_status: diElement('diPolicyStatus').value,
  turnaround_minutes: Number(diElement('diTurnaround').value), recovery_minutes: Number(diElement('diRecovery').value)
 };
}

function diPercent(probability) {
 if (typeof probability !== 'number' || !Number.isFinite(probability)) throw new Error('概率响应无效');
 return new Intl.NumberFormat('zh-CN', {style: 'percent', maximumFractionDigits: 1}).format(probability);
}

function diTime(value) {
 if (value == null) return '未公布';
 const time = new Date(value);
 if (!Number.isFinite(time.getTime())) throw new Error('航班时刻响应无效');
 return new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'}).format(time);
}

function diSourceLink(value, label) {
 if (!value) return '';
 const url = new URL(value);
 if (url.protocol !== 'https:' || url.hostname !== 'zh.flightaware.com') throw new Error('数据来源链接无效');
 return '<a href="' + he(url.href) + '" target="_blank" rel="noopener">' + he(label) + ' ↗</a>';
}

function diWeatherHtml(item, title, summary) {
 if (item.status !== 'available') return '<article><h4>' + he(title) + '</h4><p class="di-unavailable">' + he(item.error) + '</p></article>';
 const data = item.data;
 const hours = data.hours;
 const rain = Math.max(...hours.map(hour => hour.precipitation_mm));
 const wind = Math.max(...hours.map(hour => hour.wind_speed_kmh));
 const gusts = hours.map(hour => hour.wind_gusts_kmh).filter(value => value != null);
 return '<article><h4>' + he(title + ' · ' + data.airport) + '</h4>'
  + (summary ? '<p>' + he(summary.label) + '</p>' : '')
  + '<p>每小时降水最高 ' + he(String(rain)) + ' mm · 风速最高 ' + he(String(wind)) + ' km/h'
  + (gusts.length ? ' · 阵风最高 ' + he(String(Math.max(...gusts))) + ' km/h' : ' · 阵风未提供') + '</p>'
  + '<p class="di-help">' + he(data.source + ' · 查询于 ' + diTime(data.fetched_at)) + '</p></article>';
}

function diPreviousHtml(previous) {
 if (previous.status !== 'available') return '<article><h4>同机前序航班</h4><p class="di-unavailable">' + he(previous.reason) + '</p><p>当前概率仅使用已取得的历史与天气信息。</p></article>';
 const flight = previous.flight;
 return '<article><h4>同机前序 · ' + he(flight.flight_no) + '</h4>'
  + '<p>' + he(flight.departure_airport + ' → ' + flight.arrival_airport) + '</p>'
  + '<p>计划到港 ' + he(diTime(flight.scheduled_arrival)) + '<br>'
  + he(flight.actual_arrival ? '实际到港 ' + diTime(flight.actual_arrival) : '预计到港 ' + diTime(flight.estimated_arrival)) + '</p>'
  + diSourceLink(previous.source_url, '查看前序动态') + '</article>';
}

function diEvidenceHtml(result) {
 const flight = result.flight;
 const summaries = result.estimate ? result.estimate.weather_summary : [];
 const summary = airport => summaries.find(item => item.airport === airport);
 return '<div class="di-evidence"><article><h4>' + he(flight.flight_no + ' · ' + flight.departure_airport + ' → ' + flight.arrival_airport) + '</h4>'
  + '<p>计划离港 ' + he(diTime(flight.scheduled_departure)) + '<br>计划到港 ' + he(diTime(flight.scheduled_arrival)) + '</p>'
  + '<p>预计离港 ' + he(diTime(flight.estimated_departure)) + '<br>预计到港 ' + he(diTime(flight.estimated_arrival)) + '</p>'
  + diSourceLink(result.flight_source.source_url, '查看本班动态') + '</article>'
  + diPreviousHtml(result.previous)
  + diWeatherHtml(result.weather.departure, '出发机场天气', summary(flight.departure_airport))
  + diWeatherHtml(result.weather.arrival, '到达机场天气', summary(flight.arrival_airport)) + '</div>';
}

function diMetric(label, value) { return '<div class="di-metric"><span>' + he(label) + '</span><b>' + he(value) + '</b></div>'; }

function diInsuranceHtml(insurance) {
 const advice = insurance.recommendation;
 let html = '<div class="di-advice"><b>' + he(advice.label) + '</b><p>' + he(advice.reason) + '</p></div>';
 if (insurance.recoup_probability == null) return html;
 html += '<p class="di-help">保费 ¥' + he(fqMoney(insurance.premium)) + ' · 达到门槛赔付 ¥' + he(fqMoney(insurance.payout)) + '</p>';
 const condition = insurance.conditional ? '（条款满足时）' : '';
 const net = insurance.expected_net;
 const netText = (net > 0 ? '+' : net < 0 ? '−' : '') + '¥' + fqMoney(Math.abs(net));
 html += '<div class="di-metrics">' + diMetric('回本概率' + condition, diPercent(insurance.recoup_probability))
  + diMetric('回本所需概率', insurance.break_even_probability == null ? '无赔付' : diPercent(insurance.break_even_probability))
  + diMetric('预期净收益' + condition, netText) + '</div>';
 if (insurance.conditional) html += '<p class="di-help">回本与预期收益按投保有效、当前风险在承保范围内计算；请核对保单的投保截止时间和除外条款。</p>';
 return html;
}

function diRender(result) {
 if (!result || !result.flight || !result.weather) throw new Error('延误险评估响应格式无效');
 let html = '';
 if (result.estimate) {
  const estimate = result.estimate;
  html = '<div class="di-probability">约 ' + he(diPercent(estimate.delay_probability)) + '</div>'
   + '<p class="di-help">' + he((estimate.claim_basis === 'arrival' ? '到港' : '离港') + '延误达到 ' + estimate.threshold_minutes + ' 分钟的估算概率') + '</p>'
   + '<p class="di-help">情景范围 ' + he(diPercent(estimate.probability_range[0]) + '–' + diPercent(estimate.probability_range[1]))
   + ' · ' + he(String(estimate.sample_count)) + ' 次已完成航班样本 · ' + (estimate.confidence === 'low' ? '粗略估算' : '前序与天气已取得') + '</p>'
   + diInsuranceHtml(result.insurance);
 }
 html += diEvidenceHtml(result);
 if (result.estimate) {
  const estimate = result.estimate;
  html += '<details class="di-notes"><summary>查看估算依据</summary><ul>'
   + estimate.factors.concat(estimate.assumptions).map(note => '<li>' + he(note) + '</li>').join('') + '</ul></details>';
 }
 diElement('delayInsuranceResult').innerHTML = html;
 diElement('delayInsuranceResult').scrollIntoView({block: 'start'});
}

async function diSubmit(event) {
 event.preventDefault();
 const active = DELAY_INSURANCE_ACTIVE;
 if (!active) throw new Error('尚未打开延误险评估');
 const revision = active.revision;
 const button = diElement('delayInsuranceSubmit');
 button.disabled = true;
 diElement('delayInsuranceError').textContent = '';
 diElement('delayInsuranceResult').textContent = '';
 const started = Date.now();
 active.controller = new AbortController();
 const progress = () => { diElement('delayInsuranceProgress').textContent = '正在自动查询航班、前序与机场天气… ' + Math.floor((Date.now() - started) / 1000) + ' 秒'; };
 progress(); active.progressTimer = setInterval(progress, 1000);
 active.timeoutTimer = setTimeout(() => active.controller.abort(new Error('自动查询超时，请稍后重试。')), DELAY_INSURANCE_REQUEST_TIMEOUT_MS);
 try {
  const response = await fetch('/api/delay-insurance/assess', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(diPayload()), signal: active.controller.signal});
  const result = await response.json();
  if (DELAY_INSURANCE_ACTIVE !== active) return;
  if (active.revision !== revision) { diElement('delayInsuranceResult').textContent = '条件已修改，请重新评估。'; return; }
  if (result.result) diRender(result.result);
  if (!response.ok || !result.ok) throw new Error(result.error || result.err || '延误险自动查询失败：HTTP ' + response.status);
  if (!result.result) throw new Error('延误险评估结果缺失');
 } catch (error) {
  if (DELAY_INSURANCE_ACTIVE === active && active.revision === revision) {
   console.error('延误险评估失败', error);
   diElement('delayInsuranceError').textContent = error.message || String(error);
  }
 } finally {
  clearInterval(active.progressTimer); clearTimeout(active.timeoutTimer);
  if (DELAY_INSURANCE_ACTIVE === active) { button.disabled = false; diElement('delayInsuranceProgress').textContent = ''; }
 }
}

diElement('delayInsuranceForm').addEventListener('submit', diSubmit);
diElement('delayInsuranceForm').addEventListener('input', diInvalidate);
diElement('delayInsuranceDialog').addEventListener('close', () => {
 diStopRequest(DELAY_INSURANCE_ACTIVE); DELAY_INSURANCE_ACTIVE = null;
});
