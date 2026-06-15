#!/bin/sh
set -u

REPORT_DIR="/app/data/relatorios"
STARTED_AT="$(date --iso-8601=seconds)"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
REPORT_PATH="${REPORT_DIR}/migracao_supabase_pgloader_${TIMESTAMP}.md"
SUMMARY_PATH="${REPORT_DIR}/migracao_supabase_pgloader_${TIMESTAMP}.json"

mkdir -p "$REPORT_DIR"

if [ -z "${COTACOES_SUPABASE_DATABASE_URL:-}" ]; then
    STATUS="Encerrada com erro"
    ERROR_MESSAGE="COTACOES_SUPABASE_DATABASE_URL nao configurada."
    EXIT_CODE=1
elif [ ! -f "/app/data/cotacoes.sqlite" ]; then
    STATUS="Encerrada com erro"
    ERROR_MESSAGE="Banco SQLite nao encontrado em data/cotacoes.sqlite."
    EXIT_CODE=1
else
    pgloader \
        --quiet \
        --on-error-stop \
        --summary "$SUMMARY_PATH" \
        --with "include no drop" \
        --with "create no tables" \
        --with "truncate" \
        --with "reset sequences" \
        --after /app/scripts/pgloader_after.sql \
        "sqlite:///app/data/cotacoes.sqlite" \
        "$COTACOES_SUPABASE_DATABASE_URL"
    EXIT_CODE=$?

    if [ "$EXIT_CODE" -eq 0 ]; then
        STATUS="Concluida sem avisos"
        ERROR_MESSAGE="Nenhum erro registrado."
    else
        STATUS="Encerrada com erro"
        ERROR_MESSAGE="pgloader encerrou com codigo ${EXIT_CODE}."
    fi
fi

FINISHED_AT="$(date --iso-8601=seconds)"

{
    echo "# Relatorio de execucao: Migracao completa com pgloader"
    echo
    echo "## Resumo executivo"
    echo
    echo "- Inicio: \`${STARTED_AT}\`"
    echo "- Fim: \`${FINISHED_AT}\`"
    echo "- Status: **${STATUS}**"
    echo "- Origem: \`data/cotacoes.sqlite\`"
    echo "- Destino: \`Supabase PostgreSQL\`"
    echo "- Estrategia: \`pgloader create no tables, truncate, reset sequences\`"
    echo
    echo "## Erros"
    echo
    echo "- ${ERROR_MESSAGE}"
    echo
    echo "## Resumo do pgloader"
    echo

    if [ -f "$SUMMARY_PATH" ]; then
        echo "\`\`\`json"
        cat "$SUMMARY_PATH"
        echo "\`\`\`"
    else
        echo "Resumo do pgloader nao gerado."
    fi
} > "$REPORT_PATH"

echo "Relatorio salvo em ${REPORT_PATH}."
exit "$EXIT_CODE"
