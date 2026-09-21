<script setup>
import { computed, ref, watch } from 'vue'

const fallbackProviders = {
  ollama: {
    models: ['qwen2.5:1.5b', 'qwen2.5-coder:3b'],
    available: true,
    reason: '',
  },
  mistral: {
    models: ['magistral-medium-latest'],
    available: false,
    reason: 'Mistral provider configuration is unavailable. Check the API server.',
  },
}

const providers = ref({ ...fallbackProviders })
const caseDir = ref('examples/hdr_timeout')
const provider = ref('ollama')
const model = ref(fallbackProviders.ollama.models[0])
const feedbackText = ref('')
const feedbackHistory = ref([])
const showFeedbackInput = ref(false)
const loading = ref(false)
const error = ref('')
const result = ref(null)
const diff = ref('')
const appliedCount = ref(0)
const actionMode = ref('idle')

const selectedProviderReason = computed(() => {
  return providers.value[provider.value]?.reason || ''
})

const confidenceText = computed(() => {
  if (!result.value) return '—'
  return `${Number(result.value.confidence).toFixed(1)}%`
})

watch(provider, (nextProvider) => {
  const nextModels = providers.value[nextProvider]?.models || []
  model.value = nextModels[0] || ''
})

async function runInvestigation(previousResult = null) {
  loading.value = true
  error.value = ''
  result.value = null
  diff.value = ''
  actionMode.value = 'idle'
  showFeedbackInput.value = false

  try {
    const response = await fetch('/api/investigate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        case_dir: caseDir.value,
        provider: provider.value,
        model: model.value,
        feedback: feedbackHistory.value.join('\n') || null,
        previous_result: previousResult,
      }),
    })

    let payload = null
    try {
      payload = await response.json()
    } catch {
      const text = await response.text()
      throw new Error(text || 'Server returned invalid JSON.')
    }

    if (!response.ok) {
      throw new Error(payload?.detail || payload?.error || 'Investigation failed.')
    }

    result.value = payload.result
    diff.value = payload.diff || ''
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'Unexpected error occurred.'
  } finally {
    loading.value = false
  }
}

async function applyAcceptedChanges() {
  try {
    const response = await fetch('/api/apply', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        case_dir: caseDir.value,
        changes: result.value.proposed_changes,
      }),
    })

    let payload = null
    try {
      payload = await response.json()
    } catch {
      const text = await response.text()
      throw new Error(text || 'Server returned invalid JSON.')
    }

    if (!response.ok) {
      throw new Error(payload?.detail || payload?.error || 'Failed to apply changes.')
    }

    appliedCount.value = payload.count || 0
    actionMode.value = 'accepted'
    result.value = null
    diff.value = ''
    showFeedbackInput.value = false
  } catch (err) {
    error.value = err instanceof Error ? err.message : 'Failed to apply changes.'
  }
}

function submitFeedback() {
  const value = feedbackText.value.trim()
  if (!value) {
    return
  }

  const previousResult = result.value
  feedbackHistory.value.push(value)
  feedbackText.value = ''
  runInvestigation(previousResult)
}

function startInvestigation() {
  feedbackHistory.value = []
  feedbackText.value = ''
  runInvestigation()
}

function quitSession() {
  actionMode.value = 'quit'
  result.value = null
  diff.value = ''
  appliedCount.value = 0
}

function toggleReviewInput() {
  showFeedbackInput.value = !showFeedbackInput.value
}

async function loadProviderOptions() {
  try {
    const response = await fetch('/api/providers')
    if (!response.ok) {
      return
    }

    const payload = await response.json()
    const nextProviders = payload.providers || {}
    if (Object.keys(nextProviders).length) {
      providers.value = nextProviders
    }

    const availableProviders = Object.entries(providers.value).filter(([, meta]) => meta.available)
    const selected = availableProviders[0]?.[0] || 'ollama'
    provider.value = selected
    model.value = providers.value[selected]?.models?.[0] || ''
  } catch {
    providers.value = { ...fallbackProviders }
    provider.value = 'ollama'
    model.value = fallbackProviders.ollama.models[0]
  }
}

loadProviderOptions()
</script>

<template>
  <main class="app-shell">
    <section class="panel controls">
      <h1>RootPilot</h1>
      <p class="subtitle">AI-assisted investigation dashboard</p>

      <div class="field-grid">
        <label>
          Case directory
          <input v-model="caseDir" type="text" placeholder="examples/hdr_timeout" />
        </label>

        <label>
          Provider
          <select v-model="provider">
            <option
              v-for="(meta, key) in providers"
              :key="key"
              :value="key"
              :disabled="!meta.available"
            >
              {{ key }}{{ meta.available ? '' : ' (unavailable)' }}
            </option>
          </select>
        </label>

        <label>
          Model
          <select v-model="model">
            <option v-for="option in (providers[provider]?.models || [])" :key="option" :value="option">
              {{ option }}
            </option>
            <option v-if="!(providers[provider]?.models || []).length" value="">(no models available)</option>
          </select>
        </label>
      </div>

      <button class="primary" :disabled="loading || !providers[provider]?.available" @click="startInvestigation">
        {{ loading ? 'Investigating...' : 'Run investigation' }}
      </button>
    </section>

    <section v-if="selectedProviderReason" class="panel warning-box">
      {{ selectedProviderReason }}
    </section>

    <section v-if="error" class="panel error-box">
      {{ error }}
    </section>

    <section v-if="actionMode === 'quit'" class="panel info-box">
      Investigation session ended.
    </section>

    <section v-if="actionMode === 'accepted'" class="panel success-box">
      Applied {{ appliedCount }} change(s).
    </section>

    <section v-if="result" class="panel result-panel">
      <header class="section-header">
        <h2>Investigation result</h2>
        <span class="badge">Confidence {{ confidenceText }}</span>
      </header>

      <div class="result-block">
        <h3>Evidence summary</h3>
        <p>{{ result.evidence_summary }}</p>
      </div>

      <div class="result-block">
        <h3>Hypotheses</h3>
        <ol>
          <li v-for="(item, index) in result.hypotheses" :key="index">{{ item }}</li>
        </ol>
      </div>

      <div class="result-block">
        <h3>Recommended investigation</h3>
        <p>{{ result.recommended_investigation }}</p>
      </div>

      <div v-if="diff" class="result-block">
        <h3>Proposed changes</h3>
        <div class="diff-viewer">
          <template v-for="(line, idx) in diff.split('\n')" :key="idx">
            <div
              :class="{
                'diff-line': true,
                'diff-add': line.startsWith('+') && !line.startsWith('+++'),
                'diff-del': line.startsWith('-') && !line.startsWith('---'),
                'diff-hunk': line.startsWith('@@'),
                'diff-meta': line.startsWith('+++') || line.startsWith('---') || line.startsWith('diff ')
              }"
            >
              <code>{{ line }}</code>
            </div>
          </template>
        </div>
      </div>

      <div class="action-row">
        <button class="action-btn success" @click="applyAcceptedChanges">Accept</button>
        <button class="action-btn neutral" @click="toggleReviewInput">Review</button>
        <button class="action-btn warn" @click="quitSession">Quit</button>
      </div>

      <div v-if="showFeedbackInput" class="feedback-box">
        <label>
          Additional evidence
          <textarea v-model="feedbackText" rows="3" placeholder="Add logs, test result, or new findings"></textarea>
        </label>
        <button class="primary small" @click="submitFeedback">Submit feedback</button>
      </div>

      <div v-if="feedbackHistory.length" class="history-box">
        <h3>Feedback history</h3>
        <ul>
          <li v-for="(item, index) in feedbackHistory" :key="index">{{ item }}</li>
        </ul>
      </div>
    </section>
  </main>
</template>

<style scoped>
.app-shell {
  max-width: 1100px;
  margin: 0 auto;
  padding: 32px 20px 60px;
  display: grid;
  gap: 20px;
}

.panel {
  background: #111827;
  border: 1px solid #2a3245;
  border-radius: 16px;
  padding: 24px;
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.18);
}

.controls h1 {
  margin: 0;
  font-size: 2.2rem;
}

.subtitle {
  color: #9ca3af;
  margin: 8px 0 20px;
}

.field-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 16px;
}

label {
  display: flex;
  flex-direction: column;
  gap: 8px;
  color: #d1d5db;
  font-weight: 600;
}

input,
select,
textarea,
button {
  font: inherit;
}

input,
select,
textarea {
  background: #0f172a;
  color: #f9fafb;
  border: 1px solid #334155;
  border-radius: 10px;
  padding: 10px 12px;
}

textarea {
  resize: vertical;
}

.primary,
.action-btn {
  border: none;
  border-radius: 10px;
  padding: 12px 18px;
  cursor: pointer;
  font-weight: 700;
}

.primary {
  margin-top: 20px;
  background: linear-gradient(135deg, #8b5cf6, #3b82f6);
  color: white;
}

.primary.small {
  margin-top: 12px;
  width: fit-content;
}

.primary:disabled {
  opacity: 0.7;
  cursor: wait;
}

.section-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}

.section-header h2 {
  margin: 0;
}

.badge {
  background: rgba(139, 92, 246, 0.2);
  color: #d8b4fe;
  border: 1px solid rgba(139, 92, 246, 0.6);
  border-radius: 999px;
  padding: 6px 10px;
  font-size: 0.85rem;
  font-weight: 700;
}

.result-block {
  margin-top: 18px;
}

.result-block h3 {
  margin: 0 0 8px;
  color: #e5e7eb;
}

.result-block p,
.result-block li,
.history-box li {
  color: #d1d5db;
  line-height: 1.6;
}

pre {
  white-space: pre-wrap;
  background: rgba(15, 23, 42, 0.8);
  border: 1px solid #334155;
  border-radius: 12px;
  padding: 16px;
  color: #e2e8f0;
  overflow-x: auto;
}

.action-row {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  margin-top: 20px;
}

.action-btn.success {
  background: #22c55e;
  color: #06220d;
}

.action-btn.neutral {
  background: #475569;
  color: white;
}

.action-btn.warn {
  background: #ef4444;
  color: white;
}

.feedback-box {
  margin-top: 20px;
  display: grid;
  gap: 10px;
}

.history-box {
  margin-top: 20px;
}

.warning-box {
  border-color: rgba(245, 158, 11, 0.8);
  color: #fcd34d;
}

.error-box {
  border-color: rgba(239, 68, 68, 0.8);
  color: #fecaca;
}

.info-box,
.success-box {
  border-color: rgba(59, 130, 246, 0.7);
  color: #dbeafe;
}
</style>
