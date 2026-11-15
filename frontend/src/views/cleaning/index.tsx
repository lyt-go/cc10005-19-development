import { useCallback, useEffect, useState } from 'react'

import { fetchJson, request } from '@/api/client'

const ENDPOINT = '/api/cleaning'

type Row = Record<string, unknown>
type Batch = {
  id: number
  批次编号: string
  模板编号: string
  模板版本: string
  字段结果数: number
  清洗结果数: number
  冲突裁决数: number
  冻结时间: string
  abnormal: boolean
  异常明细?: { 字段: string; 说明: string }[]
  默认值与脱敏优先级?: string
}
type FieldResult = Row & { 字段名: string; 记录序号: number; 字段类型: string; 解析值: unknown; XPath版本?: string; 类型转换版本?: string }
type CleanResult = Row & {
  字段名: string
  记录序号: number
  字段类型: string
  清洗值: string[]
  清洗值序列: string[]
  多值分隔符: string
  多值项数: number
  去噪规则编号: string
  去噪规则版本: string
  使用默认填充值: boolean
  空值原因: string
  执行轨迹?: Record<string, unknown>
}
type VerifyReport = {
  批次编号: string
  模板版本: string
  指纹: string
  字段结果行数: number
  清洗结果行数: number
  一致: boolean
  问题: { 级别: string; 字段?: string; 规则?: string; 说明: string }[]
}

function openFile(path: string) {
  window.open(path, '_blank')
}

export default function CleaningBatchView() {
  const [batches, setBatches] = useState<Batch[]>([])
  const [templates, setTemplates] = useState<Row[]>([])
  const [selected, setSelected] = useState<Batch | null>(null)
  const [fields, setFields] = useState<FieldResult[]>([])
  const [cleans, setCleans] = useState<CleanResult[]>([])
  const [verify, setVerify] = useState<VerifyReport | null>(null)
  const [messages, setMessages] = useState<{ level: 'info' | 'error'; text: string }[]>([])
  const [templateId, setTemplateId] = useState('')

  const reload = useCallback(async () => {
    const [batchPage, templatePage] = await Promise.all([
      fetchJson<{ items: Batch[]; total: number }>(`${ENDPOINT}/batches?size=200`),
      fetchJson<{ items: Row[]; total: number }>('/api/parser?size=200'),
    ])
    setBatches(batchPage.items ?? [])
    setTemplates(templatePage.items ?? [])
  }, [])

  useEffect(() => {
    void reload().catch(() => setMessages([{ level: 'error', text: '清洗批次列表读取失败' }]))
  }, [reload])

  const loadBatch = useCallback(async (batch: Batch) => {
    setSelected(batch)
    setVerify(null)
    try {
      const [fieldPayload, cleanPayload, verifyPayload] = await Promise.all([
        fetchJson<{ items: FieldResult[] }>(`${ENDPOINT}/batches/${batch.id}/field-results`),
        fetchJson<{ items: CleanResult[] }>(`${ENDPOINT}/batches/${batch.id}/clean-results`),
        fetchJson<VerifyReport>(`${ENDPOINT}/batches/${batch.id}/verify`),
      ])
      setFields(fieldPayload.items ?? [])
      setCleans(cleanPayload.items ?? [])
      setVerify(verifyPayload)
    } catch {
      setMessages([{ level: 'error', text: '批次结果读取失败，请稍后重试' }])
    }
  }, [])

  async function submitTemplate() {
    const id = templateId.trim()
    if (!id) {
      setMessages([{ level: 'error', text: '请输入解析模板记录 id（如 PARS-0002 对应 id=2）' }])
      return
    }
    try {
      const response = await request(`/api/parser/${id}/submit`, { method: 'POST' })
      const payload = await response.json()
      if (!payload.ok) {
        setMessages([{ level: 'error', text: payload.message ?? '提交未生效' }])
        return
      }
      setMessages([{ level: 'info', text: payload.message }])
      await reload()
      if (payload.entry) await loadBatch(payload.entry as Batch)
    } catch (error) {
      setMessages([{ level: 'error', text: error instanceof Error ? error.message : '提交解析模板失败' }])
    }
  }

  return (
    <section className="page" data-module="cleaning">
      <header className="page-head">
        <div>
          <h2>清洗批次</h2>
          <p className="page-desc">
            提交解析模板时冻结目标字段、XPath、类型转换规则与去噪规则版本，生成可逐行核对的解析字段结果与清洗结果；
            规则改版形成新批次，旧批次保持原样。
          </p>
        </div>
      </header>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void submitTemplate() }}>
        <label className="filter-item">
          <span>提交解析模板</span>
          <select value={templateId} onChange={(event) => setTemplateId(event.target.value)}>
            <option value="">选择模板</option>
            {templates.map((row) => (
              <option key={String(row.id)} value={String(row.id)}>
                {String(row.模板编号)} · {String(row.模板版本 ?? '未编版本')} · {String(row.status ?? '')}
              </option>
            ))}
          </select>
        </label>
        <button className="btn primary" type="submit">冻结口径并生成批次</button>
        <button className="btn ghost" type="button" onClick={() => void reload()}>刷新批次</button>
      </form>

      {messages.length > 0 && (
        <div className="notice-stack">
          {messages.map((item, index) => (
            <p key={index} className={item.level === 'error' ? 'error-text' : 'notice-text'}>{item.text}</p>
          ))}
        </div>
      )}

      <table className="data-table">
        <thead>
          <tr>
            <th>批次编号</th>
            <th>模板编号</th>
            <th>模板版本</th>
            <th>字段结果</th>
            <th>清洗结果</th>
            <th>冲突裁决</th>
            <th>异常</th>
            <th>冻结时间</th>
            <th>交付</th>
          </tr>
        </thead>
        <tbody>
          {batches.map((batch) => (
            <tr key={batch.id} className={selected?.id === batch.id ? 'row-selected' : ''}>
              <td>
                <button className="link" type="button" onClick={() => void loadBatch(batch)}>{batch.批次编号}</button>
              </td>
              <td>{batch.模板编号}</td>
              <td>{batch.模板版本}</td>
              <td>{batch.字段结果数} 行</td>
              <td>{batch.清洗结果数} 行</td>
              <td>{batch.冲突裁决数} 条</td>
              <td>{batch.abnormal ? '有异常' : '无'}</td>
              <td>{batch.冻结时间}</td>
              <td className="row-actions">
                <button className="link" type="button" onClick={() => openFile(`/api/delivery/batches/${batch.id}/export`)}>导出</button>
                <button className="link" type="button" onClick={() => openFile(`/api/delivery/batches/${batch.id}/download`)}>下载</button>
                <button className="link" type="button" onClick={() => openFile(`/api/delivery/batches/${batch.id}/package`)}>打包</button>
              </td>
            </tr>
          ))}
          {batches.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={9}>暂无清洗批次，先选择一个解析模板提交</td>
            </tr>
          )}
        </tbody>
      </table>

      {selected && (
        <section className="batch-detail">
          <header className="page-head">
            <div>
              <h3>{selected.批次编号}</h3>
              <p className="page-desc">模板版本 {selected.模板版本}；{selected.默认值与脱敏优先级}</p>
            </div>
            {verify && (
              <span className={verify.一致 ? 'badge ok' : 'badge error'}>
                {verify.一致 ? '两侧结果一致' : '存在不一致项'} · 指纹 {verify.指纹}
              </span>
            )}
          </header>

          {selected.异常明细 && selected.异常明细.length > 0 && (
            <ul className="issue-list">
              {selected.异常明细.map((item) => (
                <li key={item.说明}>{item.字段}：{item.说明}</li>
              ))}
            </ul>
          )}

          {verify && verify.问题.length > 0 && (
            <ul className="issue-list">
              {verify.问题.map((issue, index) => (
                <li key={index}>[{issue.级别}] {issue.字段 ?? issue.规则 ?? ''} {issue.说明}</li>
              ))}
            </ul>
          )}

          <h4>解析字段结果</h4>
          <table className="data-table">
            <thead>
              <tr>
                <th>字段</th>
                <th>记录</th>
                <th>字段类型</th>
                <th>解析值</th>
                <th>XPath版本</th>
                <th>类型转换版本</th>
                <th>默认值版本</th>
                <th>分隔符版本</th>
              </tr>
            </thead>
            <tbody>
              {fields.map((row) => (
                <tr key={`${row.字段名}-${row.记录序号}`}>
                  <td>{row.字段名}</td>
                  <td>{row.记录序号}</td>
                  <td>{row.字段类型}</td>
                  <td>{String(row.解析值 ?? '—')}</td>
                  <td>{row.XPath版本}</td>
                  <td>{row.类型转换版本}</td>
                  <td>{String(row.默认值版本 ?? '')}</td>
                  <td>{String(row.分隔符版本 ?? '')}</td>
                </tr>
              ))}
              {fields.length === 0 && <tr><td className="empty-state" colSpan={8}>暂无解析字段结果</td></tr>}
            </tbody>
          </table>

          <h4>清洗结果</h4>
          <table className="data-table">
            <thead>
              <tr>
                <th>字段</th>
                <th>记录</th>
                <th>字段类型</th>
                <th>清洗值</th>
                <th>多值项数</th>
                <th>分隔符</th>
                <th>去噪规则</th>
                <th>规则版本</th>
                <th>默认填充值</th>
                <th>空值原因</th>
              </tr>
            </thead>
            <tbody>
              {cleans.map((row) => (
                <tr key={`${row.字段名}-${row.记录序号}`}>
                  <td>{row.字段名}</td>
                  <td>{row.记录序号}</td>
                  <td>{row.字段类型}</td>
                  <td>{row.清洗值序列.length > 0 ? row.清洗值序列.join(row.多值分隔符 || ' / ') : '—'}</td>
                  <td>{row.多值项数}</td>
                  <td>{row.多值分隔符 || '—'}</td>
                  <td>{row.去噪规则编号 || '—'}</td>
                  <td>{row.去噪规则版本 || '—'}</td>
                  <td>{row.使用默认填充值 ? '是' : '否'}</td>
                  <td>{row.空值原因 || '—'}</td>
                </tr>
              ))}
              {cleans.length === 0 && <tr><td className="empty-state" colSpan={10}>暂无清洗结果</td></tr>}
            </tbody>
          </table>

          <h4>冲突裁决</h4>
          <table className="data-table">
            <thead>
              <tr>
                <th>字段</th>
                <th>规则编号</th>
                <th>规则版本</th>
                <th>优先级</th>
                <th>正则用途</th>
                <th>是否生效</th>
                <th>裁决理由</th>
              </tr>
            </thead>
            <tbody>
              {(selected as unknown as { 冲突裁决?: Record<string, unknown>[] }).冲突裁决?.map((note) => (
                <tr key={`${note.字段}-${note.规则编号}`} className={note.是否生效 ? 'row-selected' : ''}>
                  <td>{String(note.字段)}</td>
                  <td>{String(note.规则编号)}</td>
                  <td>{String(note.规则版本)}</td>
                  <td>{String(note.优先级)}</td>
                  <td>{String(note.正则用途)}</td>
                  <td>{note.是否生效 ? '生效' : '未生效'}</td>
                  <td>{String(note.裁决理由)}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <h4>冻结的去噪规则版本</h4>
          <table className="data-table">
            <thead>
              <tr>
                <th>规则编号</th>
                <th>规则版本</th>
                <th>正则匹配式</th>
                <th>正则版本</th>
                <th>默认填充值</th>
                <th>默认值版本</th>
                <th>脱敏替换模板</th>
                <th>脱敏版本</th>
                <th>优先级</th>
              </tr>
            </thead>
            <tbody>
              {Object.values((selected as unknown as { 去噪规则?: Record<string, Row> }).去噪规则 ?? {}).map((rule) => (
                <tr key={String(rule.规则编号)}>
                  <td>{String(rule.规则编号)}</td>
                  <td>{String(rule.规则版本 ?? '')}</td>
                  <td>{String(rule.正则匹配式 ?? '')}</td>
                  <td>{String(rule.正则版本 ?? '')}</td>
                  <td>{String(rule.默认填充值 ?? '')}</td>
                  <td>{String(rule.默认值版本 ?? '')}</td>
                  <td>{String(rule.脱敏替换模板 ?? '')}</td>
                  <td>{String(rule.脱敏版本 ?? '')}</td>
                  <td>{String(rule.优先级 ?? '')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      <footer className="page-foot">
        <span>共 {batches.length} 个清洗批次</span>
      </footer>
    </section>
  )
}
