{{/*
Expand the name of the chart.
*/}}
{{- define "voice-agent-platform.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "voice-agent-platform.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart label.
*/}}
{{- define "voice-agent-platform.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels applied to every object.
*/}}
{{- define "voice-agent-platform.labels" -}}
helm.sh/chart: {{ include "voice-agent-platform.chart" . }}
{{ include "voice-agent-platform.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels (used in matchLabels — must be immutable after first deploy).
*/}}
{{- define "voice-agent-platform.selectorLabels" -}}
app.kubernetes.io/name: {{ include "voice-agent-platform.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Service account name.
*/}}
{{- define "voice-agent-platform.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "voice-agent-platform.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Image reference — prepends the global registry when set.
Usage: {{ include "voice-agent-platform.image" (dict "image" .Values.someService.image "global" .Values.global "chart" .Chart) }}
*/}}
{{- define "voice-agent-platform.image" -}}
{{- $registry := .global.imageRegistry | default "" -}}
{{- $repo := .image.repository -}}
{{- $tag := .image.tag | default .chart.AppVersion -}}
{{- if $registry -}}
{{- printf "%s/%s:%s" (trimSuffix "/" $registry) $repo $tag -}}
{{- else -}}
{{- printf "%s:%s" $repo $tag -}}
{{- end -}}
{{- end }}

{{/*
Name of the Secret that holds platform credentials.
*/}}
{{- define "voice-agent-platform.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else -}}
{{- include "voice-agent-platform.fullname" . }}-credentials
{{- end -}}
{{- end }}

{{/*
PostgreSQL connection string.  Prefers the external DSN when the sub-chart
is disabled.
*/}}
{{- define "voice-agent-platform.postgresHost" -}}
{{- if .Values.postgresql.enabled -}}
{{- printf "%s-postgresql" (include "voice-agent-platform.fullname" .) -}}
{{- else -}}
{{- required "externalDatabase.host is required when postgresql.enabled=false" .Values.externalDatabase.host -}}
{{- end -}}
{{- end }}

{{- define "voice-agent-platform.postgresPort" -}}
{{- if .Values.postgresql.enabled -}}5432{{- else -}}{{ .Values.externalDatabase.port | default 5432 }}{{- end -}}
{{- end }}

{{- define "voice-agent-platform.postgresDatabase" -}}
{{- if .Values.postgresql.enabled -}}{{ .Values.postgresql.auth.database }}{{- else -}}{{ .Values.externalDatabase.database }}{{- end -}}
{{- end }}

{{- define "voice-agent-platform.postgresUser" -}}
{{- if .Values.postgresql.enabled -}}{{ .Values.postgresql.auth.username }}{{- else -}}{{ .Values.externalDatabase.username }}{{- end -}}
{{- end }}

{{/*
Redis URL.
*/}}
{{- define "voice-agent-platform.redisUrl" -}}
{{- if .Values.redis.enabled -}}
{{- printf "redis://%s-redis-master:6379/0" (include "voice-agent-platform.fullname" .) -}}
{{- else -}}
{{- required "externalRedis.url is required when redis.enabled=false" .Values.externalRedis.url -}}
{{- end -}}
{{- end }}

{{/*
MinIO / S3 endpoint URL.
*/}}
{{- define "voice-agent-platform.s3Endpoint" -}}
{{- if .Values.minio.enabled -}}
{{- printf "http://%s-minio:9000" (include "voice-agent-platform.fullname" .) -}}
{{- else -}}
{{- required "externalObjectStorage.endpoint is required when minio.enabled=false" .Values.externalObjectStorage.endpoint -}}
{{- end -}}
{{- end }}

{{- define "voice-agent-platform.s3Bucket" -}}
{{- if .Values.minio.enabled -}}recordings{{- else -}}{{ .Values.externalObjectStorage.bucket }}{{- end -}}
{{- end }}

{{/*
Validate required secrets — fail at render time rather than at runtime.
*/}}
{{- define "voice-agent-platform.validateSecrets" -}}
{{- if not .Values.secrets.existingSecret -}}
{{- required "secrets.jwtSecret must not be empty" .Values.secrets.jwtSecret | sha256sum | quote | nindent 0 -}}
{{- required "secrets.livekitApiSecret must not be empty" .Values.secrets.livekitApiSecret | sha256sum | quote | nindent 0 -}}
{{- end -}}
{{- end }}
