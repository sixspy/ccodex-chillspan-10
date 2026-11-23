export const reconcileViewTemplate = `
<section class="panel">
  <h2>仓间对账互证</h2>
  <p class="hint">差额口径：在批 - 配额 &gt; 0 即差额未清；未清时温谱正式导出零增拒收。</p>
  <div class="form-row">
    <div><label>仓间编码</label><input v-model="reconCode"/></div>
    <div><label>批次互证编码</label><input v-model="proofCode"/></div>
  </div>
  <div class="actions">
    <button @click="loadRecon">对账单仓</button>
    <button class="secondary" @click="loadProof">批次互证</button>
    <button class="secondary" @click="loadReconAll">全仓对账</button>
  </div>
  <div v-if="reconOne" class="detail-box">
    <p>配额 {{ reconOne.quota_batches }} · 在批 {{ reconOne.active_batches }} · <span :class="reconOne.balanced ? 'ok' : 'bad'">{{ reconOne.balanced ? "平衡" : "失衡" }}</span></p>
    <p>差额 <span class="badge" :class="gapClass(reconOne)">{{ gapText(reconOne) }}</span></p>
  </div>
  <div v-if="proofOne" class="detail-box">
    <p>状态 {{ proofOne.status }} · 开放游标 {{ proofOne.open_excursions }} · 温谱 {{ proofOne.spectrum_exports }}</p>
    <p>差额 <span class="badge" :class="gapClass(proofOne.gap)">{{ gapText(proofOne.gap) }}</span></p>
  </div>
  <table class="data-table">
    <thead><tr><th>仓间</th><th>区域</th><th>在批</th><th>余配额</th><th>平衡</th><th>差额</th></tr></thead>
    <tbody>
      <tr v-for="r in reconAllItems" :key="r.chamber_code">
        <td>{{ r.chamber_code }}</td>
        <td>{{ r.region }}</td>
        <td>{{ r.active_batches }}</td>
        <td>{{ r.quota_left }}</td>
        <td>{{ r.balanced ? "是" : "否" }}</td>
        <td><span class="badge" :class="gapClass(r)">{{ r.uncleared ? "未清 " + r.gap : "已清" }}</span></td>
      </tr>
    </tbody>
  </table>
  <h3>差额导出台账（当日封口）</h3>
  <table class="data-table">
    <thead><tr><th>日期</th><th>批次</th><th>仓间</th><th>动作</th><th>状态</th><th>操作人</th></tr></thead>
    <tbody>
      <tr v-for="g in gapLedger" :key="g.id">
        <td>{{ g.day }}</td>
        <td>{{ g.batch_code }}</td>
        <td>{{ g.chamber_code }}</td>
        <td>{{ g.action }}</td>
        <td>{{ g.status }}</td>
        <td>{{ g.actor }}</td>
      </tr>
    </tbody>
  </table>
</section>
`;
