/* mindmap.js — D3.js 三层记忆思维导图渲染（依赖 d3 全局变量）
 * ADR-006: Working → Episodic → Semantic architecture projection */

const TYPE_COLORS = { preference:'#58a6ff', fact:'#3fb950', task:'#d29922', restriction:'#f85149', style:'#bc8cff' };
const LAYER_COLORS = { working: '#f0ad4e', episodic: '#5bc0de', semantic: '#6366f1' };
let svgZoom, svgG;

function renderMindmap(data, userId) {
  const svg = d3.select('#mindmapSvg');
  svg.selectAll('*').remove();
  const container = document.getElementById('panelMindmap');
  const W = container.clientWidth || 800;
  const H = container.clientHeight || 500;
  svg.attr('viewBox', `0 0 ${W} ${H}`);

  const working = data.working || [];
  const episodic = data.episodic || [];
  const semantic = data.semantic || [];
  const hasData = working.length > 0 || episodic.length > 0 || semantic.length > 0;

  if (!hasData) {
    svg.append('text').attr('x', W/2).attr('y', H/2).attr('text-anchor','middle')
      .attr('fill', 'var(--text2)').attr('font-size', 14).text('选择一个用户以查看记忆导图');
    return;
  }

  // ── Build three-tier hierarchy ──────────────────────────────────────
  const root = { name: userId || 'User', children: [] };

  const workingChildren = working.map(t => ({
    name: (t.role || '?') + ': ' + (t.content && t.content.length > 25 ? t.content.slice(0,25)+'…' : (t.content||'')),
    fullText: (t.role||'?') + ': ' + (t.content||''),
    layer: 'working', turnData: t
  }));
  root.children.push({
    name: 'Working', layer: 'working', layerType: 'layer',
    children: workingChildren.length ? workingChildren : [{ name: '(暂无数据)', layer: 'working', empty: true }]
  });

  const epChildren = episodic.map(ep => ({
    name: (ep.episode_title || 'Episode #'+ep.id),
    fullText: (ep.episode_summary || '') + '\nstatus: ' + (ep.consolidation_status||ep.status||'') + '  sources: ' + (ep.source_count||0),
    layer: 'episodic', epData: ep
  }));
  root.children.push({
    name: 'Episodic', layer: 'episodic', layerType: 'layer',
    children: epChildren.length ? epChildren : [{ name: '(暂无数据)', layer: 'episodic', empty: true }]
  });

  const semChildren = semantic.map(m => ({
    name: m.memory && m.memory.length > 28 ? m.memory.slice(0,28)+'…' : (m.memory||''),
    fullText: m.memory || '', layer: 'semantic',
    type: m.memory_type || 'fact', memData: m, importance: m.importance || 0.5
  }));
  root.children.push({
    name: 'Semantic', layer: 'semantic', layerType: 'layer',
    children: semChildren.length ? semChildren : [{ name: '(暂无数据)', layer: 'semantic', empty: true }]
  });

  // ── D3 tree layout ──────────────────────────────────────────────────
  const hierarchy = d3.hierarchy(root);
  const treeW = Math.max(W, 260 + (Math.max(working.length, episodic.length, semantic.length) + 3) * 80);
  d3.tree().size([H - 80, treeW - 260])(hierarchy);

  svgG = svg.append('g').attr('transform', 'translate(80, 40)');
  svgZoom = d3.zoom().scaleExtent([0.2, 3]).on('zoom', e => svgG.attr('transform', e.transform));
  svg.call(svgZoom).call(svgZoom.transform, d3.zoomIdentity.translate(80, 40));

  // ── Links ───────────────────────────────────────────────────────────
  svgG.selectAll('.link').data(hierarchy.links()).join('path').attr('class','link')
    .attr('d', d3.linkHorizontal().x(d => d.y).y(d => d.x))
    .attr('stroke', d => {
      if (d.target.data.layerType === 'layer') return LAYER_COLORS[d.target.data.layer] || 'var(--border)';
      if (d.target.data.layer === 'semantic') return TYPE_COLORS[d.target.data.type] || 'var(--border)';
      return LAYER_COLORS[d.target.data.layer] || 'var(--border)';
    })
    .attr('stroke-opacity', d => d.target.data.layerType === 'layer' ? 0.5 : 0.3)
    .attr('stroke-width', d => d.target.data.layerType === 'layer' ? 2 : 1.5)
    .attr('stroke-dasharray', d => d.target.data.layerType === 'layer' ? '6,3' : null);

  // ── Nodes ───────────────────────────────────────────────────────────
  const nodes = svgG.selectAll('.node').data(hierarchy.descendants()).join('g').attr('class','node')
    .attr('transform', d => `translate(${d.y},${d.x})`);

  nodes.append('circle')
    .attr('r', d => {
      if (d.depth === 0) return 14;
      if (d.data.layerType === 'layer') return 11;
      if (d.data.empty) return 5;
      if (d.data.layer === 'semantic') return 5 + (d.data.importance||0.5) * 6;
      return 6;
    })
    .attr('fill', d => {
      if (d.depth === 0) return 'var(--accent)';
      if (d.data.layerType === 'layer') return LAYER_COLORS[d.data.layer] || '#666';
      if (d.data.empty) return 'var(--text2)';
      if (d.data.layer === 'semantic') return TYPE_COLORS[d.data.type] || '#666';
      return LAYER_COLORS[d.data.layer] || '#666';
    })
    .attr('fill-opacity', d => d.data.empty ? 0.4 : (d.data.layer === 'semantic' ? 0.7 : 1))
    .attr('stroke', d => {
      if (d.depth === 0) return '#fff';
      if (d.data.layerType === 'layer') return LAYER_COLORS[d.data.layer] || '#666';
      if (d.data.layer === 'semantic') return TYPE_COLORS[d.data.type] || '#666';
      return LAYER_COLORS[d.data.layer] || '#666';
    })
    .attr('stroke-width', 1.5)
    .on('click', (e, d) => { if (d.data.memData) openEditModal(d.data.memData); })
    .append('title').text(d => d.data.fullText || d.data.name);

  nodes.append('text')
    .attr('dy', d => d.depth === 0 ? -20 : (d.data.layerType === 'layer' ? -18 : 4))
    .attr('x', d => (d.data.layerType === 'layer' || d.depth === 0) ? 0 : 12)
    .attr('text-anchor', d => (d.data.layerType === 'layer' || d.depth === 0) ? 'middle' : 'start')
    .attr('font-weight', d => (d.depth <= 1) ? 600 : 400)
    .attr('font-size', d => d.depth === 0 ? 14 : (d.data.layerType === 'layer' ? 13 : 11))
    .attr('fill', d => d.data.empty ? 'var(--text2)' : (d.data.layerType === 'layer' ? '#fff' : 'var(--text)'))
    .text(d => d.data.name);
}

function zoomIn() { d3.select('#mindmapSvg').transition().duration(300).call(svgZoom.scaleBy, 1.3); }
function zoomOut() { d3.select('#mindmapSvg').transition().duration(300).call(svgZoom.scaleBy, 0.7); }
function resetZoom() { d3.select('#mindmapSvg').transition().duration(300).call(svgZoom.transform, d3.zoomIdentity.translate(80, 40)); }
