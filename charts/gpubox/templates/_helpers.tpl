{{/*
Expand the name of the chart.
*/}}
{{- define "gpubox.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "gpubox.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := include "gpubox.name" . -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Chart label.
*/}}
{{- define "gpubox.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Selector labels.
*/}}
{{- define "gpubox.selectorLabels" -}}
app.kubernetes.io/name: {{ include "gpubox.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Common labels.
*/}}
{{- define "gpubox.labels" -}}
helm.sh/chart: {{ include "gpubox.chart" . }}
{{ include "gpubox.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
ServiceAccount name.
*/}}
{{- define "gpubox.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "gpubox.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
Service name (shared between Service and StatefulSet.serviceName).
*/}}
{{- define "gpubox.serviceName" -}}
{{- include "gpubox.fullname" . -}}
{{- end -}}

{{/*
Application image tag.
Defaults to release-style v<Chart.Version> when values.image.tag is unset.
*/}}
{{- define "gpubox.imageTag" -}}
{{- default (printf "v%s" .Chart.Version) .Values.image.tag -}}
{{- end -}}

{{/*
Application image reference.
When values.image.digest is set, render <repository>:<tag>@<digest>.
*/}}
{{- define "gpubox.imageRef" -}}
{{- $tag := include "gpubox.imageTag" . -}}
{{- if .Values.image.digest -}}
{{- printf "%s:%s@%s" .Values.image.repository $tag .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository $tag -}}
{{- end -}}
{{- end -}}

{{/*
PVC names.
*/}}
{{- define "gpubox.homeClaimName" -}}
{{- if .Values.persistence.home.existingClaim -}}
{{- .Values.persistence.home.existingClaim -}}
{{- else -}}
{{- printf "%s-home" (include "gpubox.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "gpubox.transferClaimName" -}}
{{- if .Values.persistence.transfer.existingClaim -}}
{{- .Values.persistence.transfer.existingClaim -}}
{{- else -}}
{{- printf "%s-transfer" (include "gpubox.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "gpubox.tmpClaimName" -}}
{{- if .Values.persistence.tmp.existingClaim -}}
{{- .Values.persistence.tmp.existingClaim -}}
{{- else -}}
{{- printf "%s-tmp" (include "gpubox.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
SSH authorized_keys ConfigMap name.
*/}}
{{- define "gpubox.sshAuthorizedKeysConfigMapName" -}}
{{- if .Values.ssh.existingAuthorizedKeysConfigMap -}}
{{- .Values.ssh.existingAuthorizedKeysConfigMap -}}
{{- else -}}
{{- printf "%s-ssh-authorized-keys" (include "gpubox.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
Render an extra resource value through tpl.
Supports either a string template or an object converted to YAML first.
*/}}
{{- define "gpubox.extraResourceRender" -}}
{{- $value := .value -}}
{{- $context := .context -}}
{{- if kindIs "string" $value -}}
{{- tpl $value $context -}}
{{- else -}}
{{- tpl (toYaml $value) $context -}}
{{- end -}}
{{- end -}}

{{/*
Well-known cluster-scoped kinds.
Used to avoid namespace injection for extraResources items.
*/}}
{{- define "gpubox.extraResourceClusterScopedKinds" -}}
Namespace: true
Node: true
PersistentVolume: true
StorageClass: true
PriorityClass: true
RuntimeClass: true
IngressClass: true
ClusterRole: true
ClusterRoleBinding: true
CustomResourceDefinition: true
MutatingWebhookConfiguration: true
ValidatingWebhookConfiguration: true
ValidatingAdmissionPolicy: true
ValidatingAdmissionPolicyBinding: true
APIService: true
VolumeAttachment: true
CSIDriver: true
CSINode: true
PodSecurityPolicy: true
FlowSchema: true
PriorityLevelConfiguration: true
{{- end -}}
