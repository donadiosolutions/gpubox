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
