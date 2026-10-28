<script setup>
import { computed, reactive, ref, watch } from 'vue'

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
const dataPolicy = ref('standard')
const activeRequest = ref('')
const error = ref('')
const devilReviewResponse = ref(null)

function createInvestigationState() {
  return {
    response: null,
    config: null,
    applied: false,
  }
}

const investigations = reactive({
  A: createInvestigationState(),
  B: createInvestigationState(),
})

const selectedProviderReason = computed(
  () => providers.value[provider.value]?.reason || '',
)
const busy = computed(() => Boolean(activeRequest.value))
const canRunDevilReview = computed(() => {
  const a = investigations.A
  const b = investigations.B
  return Boolean(
    !busy.value &&
      a.response &&
      b.response &&
      a.config &&
      b.config &&
      JSON.stringify(a.config) === JSON.stringify(b.config) &&
      JSON.stringify(a.config) === JSON.stringify(currentConfig()),
  )
})

watch(provider, (nextProvider) => {
  const nextModels = providers.value[nextProvider]?.models || []
  model.value = nextModels[0] || ''
})

function currentConfig() {
  return {
    case_dir: caseDir.value.trim(),
    provider: provider.value,
    model: model.value || null,
    data_policy: dataPolicy.value,
  }
}

async function readApiResponse(response, fallbackMessage) {
  const text = await response.text()
  let payload = null
  try {
    payload = text ? JSON.parse(text) : null
  } catch {
    throw new Error(text || 'Server returned invalid JSON.')
  }

  if (!response.ok) {
    const detail = payload?.detail || payload?.error
    throw new Error(typeof detail === 'string' ? detail : fallbackMessage)
  }
  return payload
}

async function runInvestigation(slot) {
  if (busy.value) return

  const state = investigations[slot]
  state.response = null
  state.config = null
  state.applied = false
  devilReviewResponse.value = null
  error.value = ''
  activeRequest.value = slot
  const config = currentConfig()

  try {
    const response = await fetch('/api/investigate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ...config,
      }),
    })
    const payload = await readApiResponse(response, `${slot} investigation failed.`)
    state.response = payload
    state.config = config
    state.applied = false
  } catch (requestError) {
    error.value =
      requestError instanceof Error ? requestError.message : 'Unexpected error occurred.'
  } finally {
    activeRequest.value = ''
  }
}

function startInvestigation(slot) {
  return runInvestigation(slot)
}

function canShowAccept(slot) {
  const state = investigations[slot]
  const response = state.response
  return Boolean(
    response?.result?.patch_decision?.status === 'allowed' &&
      response.result_id &&
      response.diff?.trim(),
  )
}

async function applyAcceptedChanges(slot) {
  if (!canShowAccept(slot) || investigations[slot].applied || busy.value) return

  const state = investigations[slot]
  activeRequest.value = `apply-${slot}`
  error.value = ''

  try {
    const response = await fetch('/api/apply', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        case_dir: state.config.case_dir,
        result_id: state.response.result_id,
      }),
    })
    const payload = await readApiResponse(response, 'Failed to apply changes.')
    state.applied = true
    state.applyMessage = `Applied ${payload.count || 0} change(s).`
  } catch (requestError) {
    error.value =
      requestError instanceof Error ? requestError.message : 'Failed to apply changes.'
  } finally {
    activeRequest.value = ''
  }
}

async function runDevilReview() {
  if (!canRunDevilReview.value) return

  activeRequest.value = 'C'
  error.value = ''
  devilReviewResponse.value = null
  try {
    const response = await fetch('/api/devil-review', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ...currentConfig(),
        report_a: investigations.A.response.result,
        report_b: investigations.B.response.result,
      }),
    })
    devilReviewResponse.value = await readApiResponse(
      response,
      'Devil Review failed.',
    )
  } catch (requestError) {
    error.value =
      requestError instanceof Error ? requestError.message : 'Devil Review failed.'
  } finally {
    activeRequest.value = ''
  }
}

function listItems(value) {
  return Array.isArray(value) ? value : []
}

function displayValue(value) {
  if (value === null || value === undefined || value === '') return 'None'
  return value
}

async function loadProviderOptions() {
  try {
    const response = await fetch('/api/providers')
    if (!response.ok) return

    const payload = await response.json()
    if (Object.keys(payload.providers || {}).length) {
      providers.value = payload.providers
    }

    const availableProvider = Object.entries(providers.value).find(
      ([, metadata]) => metadata.available,
    )?.[0]
    const selected = availableProvider || 'ollama'
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
      <p class="subtitle">A/B investigation and Devil Review workflow</p>

      <div class="field-grid">
        <label>
          Case directory
          <input
            v-model="caseDir"
            :disabled="busy"
            type="text"
            placeholder="examples/hdr_timeout"
          />
        </label>

        <label>
          Provider
          <select v-model="provider" :disabled="busy">
            <option
              v-for="(metadata, key) in providers"
              :key="key"
              :value="key"
              :disabled="!metadata.available"
            >
              {{ key }}{{ metadata.available ? '' : ' (unavailable)' }}
            </option>
          </select>
        </label>

        <label>
          Model
          <select v-model="model" :disabled="busy">
            <option
              v-for="option in providers[provider]?.models || []"
              :key="option"
              :value="option"
            >
              {{ option }}
            </option>
            <option v-if="!(providers[provider]?.models || []).length" value="">
              (no models available)
            </option>
          </select>
        </label>

        <label>
          Data policy
          <select v-model="dataPolicy" :disabled="busy">
            <option value="standard">Standard</option>
            <option value="confidential">Confidential</option>
          </select>
        </label>
      </div>

      <p v-if="selectedProviderReason" class="provider-reason">
        {{ selectedProviderReason }}
      </p>

      <div class="workflow-actions">
        <button class="primary" :disabled="busy || !providers[provider]?.available" @click="startInvestigation('A')">
          {{ activeRequest === 'A' ? 'Running A...' : 'Run A' }}
        </button>
        <button class="primary" :disabled="busy || !providers[provider]?.available" @click="startInvestigation('B')">
          {{ activeRequest === 'B' ? 'Running B...' : 'Run B' }}
        </button>
        <button class="action-btn neutral" :disabled="!canRunDevilReview" @click="runDevilReview">
          {{ activeRequest === 'C' ? 'Running Devil Review...' : 'Run Devil Review' }}
        </button>
      </div>
      <p
        v-if="(investigations.A.response || investigations.B.response) && !canRunDevilReview && !busy"
        class="workflow-hint"
      >
        Run A and B with the same case, provider, model, and data policy to enable C.
      </p>
    </section>

    <section v-if="error" class="panel error-box" role="alert">
      {{ error }}
    </section>

    <section class="investigation-grid">
      <article
        v-for="slot in ['A', 'B']"
        :key="slot"
        class="panel report-panel"
      >
        <header class="section-header">
          <h2>Investigation {{ slot }}</h2>
          <span
            v-if="investigations[slot].response?.result?.confidence !== undefined"
            class="badge"
          >
            Confidence {{ investigations[slot].response.result.confidence }}%
          </span>
        </header>

        <template v-if="investigations[slot].response">
          <template
            v-for="result in [investigations[slot].response.result]"
            :key="slot"
          >
            <div class="result-block">
              <h3>Evidence Summary</h3>
              <p>{{ displayValue(result.evidence_summary) }}</p>
            </div>

            <div
              v-for="section in [
                ['Observed Facts', 'observed_facts'],
                ['Reporter Assumptions', 'reporter_assumptions'],
                ['Competing Interpretations', 'competing_interpretations'],
                ['Hypotheses', 'hypotheses'],
              ]"
              :key="section[1]"
              class="result-block"
            >
              <h3>{{ section[0] }}</h3>
              <ul v-if="listItems(result[section[1]]).length">
                <li v-for="(item, index) in listItems(result[section[1]])" :key="index">
                  {{ item }}
                </li>
              </ul>
              <p v-else>None</p>
            </div>

            <div class="result-block">
              <h3>Design Intent Status</h3>
              <p>{{ displayValue(result.design_intent_status) }}</p>
            </div>

            <div class="result-block">
              <h3>Design Intent Evidence</h3>
              <ul v-if="listItems(result.design_intent_evidence).length">
                <li v-for="(item, index) in listItems(result.design_intent_evidence)" :key="index">
                  <strong>{{ displayValue(item.source_type) }}</strong>
                  — {{ displayValue(item.reference) }}: {{ displayValue(item.claim) }}
                </li>
              </ul>
              <p v-else>None</p>
            </div>

            <div class="result-block">
              <h3>Confidence</h3>
              <p>{{ displayValue(result.confidence) }}{{ result.confidence !== undefined ? '%' : '' }}</p>
            </div>

            <div class="result-block">
              <h3>Recommended Investigation</h3>
              <p>{{ displayValue(result.recommended_investigation) }}</p>
            </div>

            <div class="result-block">
              <h3>Patch Decision</h3>
              <p
                :class="[
                  'patch-decision',
                  result.patch_decision?.status === 'allowed' ? 'patch-allowed' : 'patch-blocked',
                ]"
              >
                {{ (result.patch_decision?.status || 'unknown').toUpperCase() }}
              </p>
              <p>{{ displayValue(result.patch_decision?.reason) }}</p>
            </div>

            <div class="result-block">
              <h3>Generation Metadata</h3>
              <ul v-if="Object.keys(result.generation_metadata || investigations[slot].response.generation_metadata || {}).length">
                <li
                  v-for="(value, key) in (result.generation_metadata || investigations[slot].response.generation_metadata)"
                  :key="key"
                >
                  {{ key }}: {{ value }}
                </li>
              </ul>
              <p v-else>None</p>
            </div>

            <div v-if="investigations[slot].response.diff?.trim()" class="result-block">
              <h3>Diff</h3>
              <pre class="diff-viewer">{{ investigations[slot].response.diff }}</pre>
            </div>

            <div v-if="canShowAccept(slot)" class="action-row">
              <button
                class="action-btn success"
                :disabled="busy || investigations[slot].applied"
                @click="applyAcceptedChanges(slot)"
              >
                {{ investigations[slot].applied ? 'Applied' : 'Accept' }}
              </button>
              <span v-if="investigations[slot].applyMessage" class="apply-message">
                {{ investigations[slot].applyMessage }}
              </span>
            </div>
          </template>

        </template>
        <p v-else class="empty-state">Run {{ slot }} to create this investigation report.</p>
      </article>
    </section>

    <section v-if="devilReviewResponse" class="panel report-panel">
      <h2>Devil Review C</h2>
      <div
        v-for="reviewSection in [
          ['Review A', devilReviewResponse.result.reviews?.A],
          ['Review B', devilReviewResponse.result.reviews?.B],
          ['Cross Review', devilReviewResponse.result.cross_review],
          ['Problem Definition Review', devilReviewResponse.result.problem_definition_review],
        ]"
        :key="reviewSection[0]"
        class="review-section"
      >
        <h3>{{ reviewSection[0] }}</h3>
        <template v-if="reviewSection[1]">
          <div
            v-for="(value, key) in reviewSection[1]"
            :key="key"
            class="result-block"
          >
            <h4>{{ key.replaceAll('_', ' ') }}</h4>
            <ul v-if="Array.isArray(value) && value.length">
              <li v-for="(item, index) in value" :key="index">{{ item }}</li>
            </ul>
            <p v-else>{{ Array.isArray(value) ? 'None' : displayValue(value) }}</p>
          </div>
        </template>
        <p v-else>None</p>
      </div>
      <div class="result-block">
        <h3>Generation Metadata</h3>
        <ul v-if="Object.keys(devilReviewResponse.generation_metadata || {}).length">
          <li v-for="(value, key) in devilReviewResponse.generation_metadata" :key="key">
            {{ key }}: {{ value }}
          </li>
        </ul>
        <p v-else>None</p>
      </div>
    </section>
  </main>
</template>

<style scoped>
.app-shell {
  max-width: 1280px;
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
  min-width: 0;
}

.controls h1 {
  margin: 0;
  font-size: 2.2rem;
}

.subtitle,
.workflow-hint,
.empty-state {
  color: #9ca3af;
}

.subtitle {
  margin: 8px 0 20px;
}

.field-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
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
  width: 100%;
  background: #0f172a;
  color: #f9fafb;
  border: 1px solid #334155;
  border-radius: 10px;
  padding: 10px 12px;
}

textarea {
  resize: vertical;
}

button {
  border: 0;
  border-radius: 10px;
  padding: 11px 16px;
  cursor: pointer;
  font-weight: 700;
}

button:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.workflow-actions,
.action-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  margin-top: 20px;
}

.primary {
  background: linear-gradient(135deg, #8b5cf6, #3b82f6);
  color: white;
}

.primary.small {
  width: fit-content;
}

.action-btn.success {
  background: #22c55e;
  color: #06220d;
}

.action-btn.neutral {
  background: #475569;
  color: white;
}

.investigation-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  align-items: start;
  gap: 20px;
}

.section-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
}

.section-header h2,
.report-panel > h2 {
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
  white-space: nowrap;
}

.result-block,
.review-section {
  margin-top: 18px;
}

.result-block h3,
.review-section h3 {
  margin: 0 0 8px;
  color: #e5e7eb;
  text-transform: capitalize;
}

.review-section h4 {
  margin: 0 0 6px;
  color: #cbd5e1;
  text-transform: capitalize;
}

.result-block p,
.result-block li,
.review-section li {
  color: #d1d5db;
  line-height: 1.6;
  overflow-wrap: anywhere;
}

.result-block ul {
  margin: 0;
  padding-left: 22px;
}

.patch-decision {
  font-weight: 800;
}

.patch-allowed {
  color: #86efac !important;
}

.patch-blocked {
  color: #fca5a5 !important;
}

.diff-viewer {
  max-height: 420px;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  background: #0b1220;
  border: 1px solid #334155;
  border-radius: 10px;
  padding: 14px;
  color: #e2e8f0;
}

.apply-message {
  color: #86efac;
}

.provider-reason {
  margin: 12px 0 0;
  color: #fcd34d;
}

.workflow-hint {
  margin: 12px 0 0;
}

.error-box {
  border-color: rgba(239, 68, 68, 0.8);
  color: #fecaca;
}

@media (max-width: 760px) {
  .investigation-grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .app-shell {
    padding: 20px 12px 40px;
  }

  .panel {
    padding: 18px;
  }
}
</style>
