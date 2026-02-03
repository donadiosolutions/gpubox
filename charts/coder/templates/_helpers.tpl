{{/*
Expand the name of the chart.
*/}}
{{- define "coder.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "coder.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := include "coder.name" . -}}
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
{{- define "coder.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Selector labels.
*/}}
{{- define "coder.selectorLabels" -}}
app.kubernetes.io/name: {{ include "coder.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Common labels.
*/}}
{{- define "coder.labels" -}}
helm.sh/chart: {{ include "coder.chart" . }}
{{ include "coder.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
ServiceAccount name.
*/}}
{{- define "coder.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "coder.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
Service name (shared between Service and StatefulSet.serviceName).
*/}}
{{- define "coder.serviceName" -}}
{{- include "coder.fullname" . -}}
{{- end -}}

{{/*
PVC names.
*/}}
{{- define "coder.homeClaimName" -}}
{{- if .Values.persistence.home.existingClaim -}}
{{- .Values.persistence.home.existingClaim -}}
{{- else -}}
{{- printf "%s-home" (include "coder.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "coder.transferClaimName" -}}
{{- if .Values.persistence.transfer.existingClaim -}}
{{- .Values.persistence.transfer.existingClaim -}}
{{- else -}}
{{- printf "%s-transfer" (include "coder.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
SSH authorized_keys ConfigMap name.
*/}}
{{- define "coder.sshAuthorizedKeysConfigMapName" -}}
{{- if .Values.ssh.existingAuthorizedKeysConfigMap -}}
{{- .Values.ssh.existingAuthorizedKeysConfigMap -}}
{{- else -}}
{{- printf "%s-ssh-authorized-keys" (include "coder.fullname" .) -}}
{{- end -}}
{{- end -}}
