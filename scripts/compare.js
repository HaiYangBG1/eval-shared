#!/usr/bin/env node

/**
 * eval-compare
 * 对比两次 PromptFoo 评估结果，生成差异报告
 * 适用于 A/B 测试、Prompt 迭代前后对比、回归检测
 *
 * 用法：eval-compare --baseline <baseline.json> --candidate <candidate.json> [--output <diff.md>]
 */

const fs = require('fs');
const path = require('path');

// ── 解析命令行参数 ──
const args = process.argv.slice(2);
const baselineIdx = args.indexOf('--baseline');
const candidateIdx = args.indexOf('--candidate');
const outputIdx = args.indexOf('--output');

if (baselineIdx === -1 || candidateIdx === -1 || !args[baselineIdx + 1] || !args[candidateIdx + 1]) {
  console.error('用法：eval-compare --baseline <baseline.json> --candidate <candidate.json> [--output <diff.md>]');
  console.error('');
  console.error('示例：');
  console.error('  eval-compare --baseline output/v1.json --candidate output/v2.json');
  process.exit(1);
}

const baselinePath = args[baselineIdx + 1];
const candidatePath = args[candidateIdx + 1];
const outputPath = outputIdx !== -1 ? args[outputIdx + 1] : 'output/compare-report.md';

// ── 工具函数 ──
function loadResults(filePath) {
  if (!fs.existsSync(filePath)) {
    console.error(`❌ 文件不存在：${filePath}`);
    process.exit(1);
  }
  const raw = JSON.parse(fs.readFileSync(filePath, 'utf-8'));
  const results = raw.results || raw;
  return Array.isArray(results) ? results : results.results || [];
}

function calcStats(results) {
  let total = 0, pass = 0, fail = 0;
  for (const r of results) {
    total++;
    const success = r.success ?? r.pass;
    if (success === true) pass++;
    else if (success === false) fail++;
  }
  return { total, pass, fail, rate: total > 0 ? ((pass / total) * 100).toFixed(1) : '0.0' };
}

function getTestKey(result) {
  // 用输入变量生成唯一键，用于匹配同一测试用例
  const vars = result.vars || {};
  return JSON.stringify(vars);
}

// ── 主逻辑 ──
function main() {
  const baseline = loadResults(baselinePath);
  const candidate = loadResults(candidatePath);
  const baseStats = calcStats(baseline);
  const candStats = calcStats(candidate);

  // 按 vars 键匹配
  const baseMap = new Map();
  for (const r of baseline) {
    baseMap.set(getTestKey(r), r);
  }

  const regressions = []; // 之前通过，现在失败
  const improvements = []; // 之前失败，现在通过
  const unchanged = { passPass: 0, failFail: 0 };

  for (const r of candidate) {
    const key = getTestKey(r);
    const base = baseMap.get(key);
    const candPass = (r.success ?? r.pass) === true;

    if (!base) continue; // 新增的测试，无法对比

    const basePass = (base.success ?? base.pass) === true;

    if (basePass && !candPass) {
      regressions.push({
        vars: r.vars || {},
        baseOutput: (base.response?.output || base.output || '').slice(0, 150),
        candOutput: (r.response?.output || r.output || '').slice(0, 150),
      });
    } else if (!basePass && candPass) {
      improvements.push({
        vars: r.vars || {},
      });
    } else if (basePass && candPass) {
      unchanged.passPass++;
    } else {
      unchanged.failFail++;
    }
  }

  // ── 生成对比报告 ──
  const rateDiff = (parseFloat(candStats.rate) - parseFloat(baseStats.rate)).toFixed(1);
  const rateEmoji = rateDiff > 0 ? '📈' : rateDiff < 0 ? '📉' : '➡️';

  const now = new Date().toISOString().replace('T', ' ').slice(0, 19);
  const lines = [
    '# 📊 评估对比报告',
    '',
    `> 生成时间：${now}`,
    `> Baseline：\`${baselinePath}\``,
    `> Candidate：\`${candidatePath}\``,
    '',
    '## 总体对比',
    '',
    '| 指标 | Baseline | Candidate | 变化 |',
    '|------|----------|-----------|------|',
    `| 总测试数 | ${baseStats.total} | ${candStats.total} | ${candStats.total - baseStats.total > 0 ? '+' : ''}${candStats.total - baseStats.total} |`,
    `| ✅ 通过 | ${baseStats.pass} | ${candStats.pass} | ${candStats.pass - baseStats.pass > 0 ? '+' : ''}${candStats.pass - baseStats.pass} |`,
    `| ❌ 失败 | ${baseStats.fail} | ${candStats.fail} | ${candStats.fail - baseStats.fail > 0 ? '+' : ''}${candStats.fail - baseStats.fail} |`,
    `| **通过率** | **${baseStats.rate}%** | **${candStats.rate}%** | **${rateEmoji} ${rateDiff > 0 ? '+' : ''}${rateDiff}%** |`,
    '',
  ];

  // 回归告警
  if (regressions.length > 0) {
    lines.push(`## 🔴 回归 (${regressions.length} 个用例)`);
    lines.push('');
    lines.push('> 以下用例在 Baseline 中通过，但在 Candidate 中失败，需要重点关注。');
    lines.push('');
    const showCount = Math.min(regressions.length, 5);
    for (let i = 0; i < showCount; i++) {
      const r = regressions[i];
      lines.push(`### ${i + 1}. 回归用例`);
      lines.push('');
      lines.push('**输入：**');
      lines.push('```json');
      lines.push(JSON.stringify(r.vars, null, 2).slice(0, 200));
      lines.push('```');
      lines.push(`**Baseline 输出：** ${r.baseOutput}...`);
      lines.push('');
      lines.push(`**Candidate 输出：** ${r.candOutput}...`);
      lines.push('');
      lines.push('---');
      lines.push('');
    }
    if (regressions.length > showCount) {
      lines.push(`> ℹ️ 还有 ${regressions.length - showCount} 个回归用例未列出。`);
      lines.push('');
    }
  }

  // 改善
  if (improvements.length > 0) {
    lines.push(`## 🟢 改善 (${improvements.length} 个用例)`);
    lines.push('');
    lines.push('> 以下用例在 Baseline 中失败，但在 Candidate 中通过。');
    lines.push('');
    const showCount = Math.min(improvements.length, 5);
    for (let i = 0; i < showCount; i++) {
      lines.push(`- ${JSON.stringify(improvements[i].vars).slice(0, 150)}`);
    }
    if (improvements.length > showCount) {
      lines.push(`- ... 还有 ${improvements.length - showCount} 个`);
    }
    lines.push('');
  }

  // 结论
  lines.push('## 结论');
  lines.push('');
  if (regressions.length === 0 && parseFloat(rateDiff) >= 0) {
    lines.push('✅ **安全升级**：无回归，通过率未下降。');
  } else if (regressions.length > 0) {
    lines.push(`⚠️ **存在回归**：${regressions.length} 个用例从 PASS 变为 FAIL，建议排查后再发布。`);
  } else {
    lines.push(`📉 **通过率下降 ${rateDiff}%**，但无可匹配的回归用例，可能是新增测试导致。`);
  }
  lines.push('');

  // 写入文件
  const dir = path.dirname(outputPath);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(outputPath, lines.join('\n'));

  console.log(`📊 对比报告已生成 → ${outputPath}`);
  console.log(`   Baseline ${baseStats.rate}% → Candidate ${candStats.rate}%  (${rateEmoji} ${rateDiff > 0 ? '+' : ''}${rateDiff}%)`);
  if (regressions.length > 0) {
    console.log(`   🔴 ${regressions.length} 个回归，${improvements.length} 个改善`);
  } else {
    console.log(`   ✅ 无回归，${improvements.length} 个改善`);
  }
}

main();
