{{- define "boss.name" -}}
boss
{{- end -}}

{{- define "boss.fullname" -}}
{{- default (include "boss.name" .) .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "boss.labels" -}}
app.kubernetes.io/name: {{ include "boss.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "boss.selectorLabels" -}}
app.kubernetes.io/name: {{ include "boss.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "boss.secretName" -}}
{{- default .Values.secrets.name .Values.auth.existingSecret -}}
{{- end -}}
