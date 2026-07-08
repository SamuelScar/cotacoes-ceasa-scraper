# Pendencias

## Resumo rapido

| Pendencia | Resumo |
| --- | --- |
| [Configurar envio de relatorio por email](#configurar-envio-de-relatorio-por-email) | Escolher um servidor SMTP gratuito/confiavel para ativar o envio ja implementado no workflow. |
| [Criterios finais de qualidade da rodada](#criterios-finais-de-qualidade-da-rodada) | Transformar os sinais ja registrados pelo workflow em uma regra objetiva de aceite. |
| [Definir resposta para fontes instaveis](#definir-resposta-para-fontes-instaveis) | Decidir quando timeouts recorrentes exigem retries, backoff ou coleta separada. |
| [Avaliar pipeline produtor-consumidor por raw](#avaliar-pipeline-produtor-consumidor-por-raw) | Considerar uma fila real entre download e persistencia se o processamento virar gargalo. |
| [Retomar Supabase no futuro](#retomar-supabase-no-futuro) | Pausar a sincronizacao enquanto o tamanho do banco excede a capacidade esperada do Supabase. |
| [Duplicacao de registros e inchaco do SQLite](#duplicacao-de-registros-e-inchaco-do-sqlite) | Resolver bug que insere cotações duplicadas e causa inchaço do banco devido a chaves dinâmicas. |

## Configurar envio de relatorio por email

O envio por e-mail ja foi parcialmente implementado no workflow por meio da
action `.github/actions/send-report-email`, usando secrets SMTP e a variavel
`COTACOES_SEND_REPORT_EMAIL`. A parte pendente nao e mais o fluxo de envio, mas
a escolha de um servidor SMTP gratuito/confiavel para operar isso sem custo.

Decisao atual:

- manter o envio opcional e desativavel por configuracao;
- nao bloquear a entrega por falta de provedor SMTP;
- retomar no futuro a pesquisa de servidor de e-mail gratuito;
- se o workflow passar a rodar varias vezes ao dia, avaliar uma janela unica de
  envio ou um marcador diario para evitar e-mails repetidos.

## Criterios finais de qualidade da rodada

Foram implementadas melhorias importantes: o workflow registra download
concluido, parcial ou falho por fonte, separa o status da persistencia, contabiliza
falhas parciais e tenta salvar os artefatos antes da validacao final. Ainda falta
transformar esses sinais em uma regra objetiva de aceite do pacote publicado.

Comportamento esperado:

1. definir limites minimos por fonte, volume ou perda aceitavel;
2. classificar a rodada como valida, parcial aproveitavel ou invalida no
   relatorio final;
3. deixar claro quando o pacote publicado nao deve substituir uma rodada boa;
4. manter os artefatos salvos para depuracao mesmo quando a rodada for invalida.

## Definir resposta para fontes instaveis

A analise dos relatorios recentes nao indicou bloqueio explicito por `403`,
`429`, `HttpSourceBlocked`, `Forbidden` ou `Too Many Requests`. Os problemas
observados ficaram mais associados a timeout, fonte instavel, limite de busca
historica e PDFs ausentes/malformados.

Pontos a investigar com mais cuidado:

1. A persistencia da CEASA-PR por `Cotacao sem data` foi tratada nos ajustes
   pos-analise registrados em `docs/entrega-final.md`; agora o ponto restante
   e acompanhar se novas rodadas confirmam a estabilidade da correcao.
2. CEASA-PR ainda pode ter PDFs ausentes em algumas datas. PDFs malformados
   passaram a ter fallback com `pdftotext`, mas vale acompanhar se novas rodadas
   ainda registram falhas reais de extracao.
3. CEASA-Campinas e CEASA-RJ aparecem com timeouts frequentes e, quando o
   download falha, a persistencia fica como `persistencia ignorada porque o
   download falhou`.
4. CEASA-GO e CEASA-CE tambem apresentam timeouts, mas com frequencia menor que
   Campinas e RJ nas coletas recentes.
5. CEASA-BA e CEAGESP-SP foram tratadas como fontes de historico limitado;
   agora a pendencia e acompanhar se os limites reduzem avisos sem perder dado
   relevante.
6. A publicacao do pacote deve considerar essas falhas parciais para evitar
   substituir um pacote bom por uma rodada com perda relevante de fontes.

Acoes futuras:

- acompanhar as proximas rodadas para confirmar que a CEASA-PR nao volta a
  falhar por `Cotacao sem data`;
- separar falha de download, falha de parser e falha de persistencia no resumo
  para deixar o impacto por fonte mais claro;
- manter CEASA-Campinas e CEASA-RJ sob observacao; retries/backoff ou coleta
  separada ficam como alternativa futura se os timeouts continuarem relevantes;
- acompanhar fontes com historico limitado, como BA e CEAGESP-SP, para ajustar
  os limites se necessario;
- acompanhar nos proximos relatorios se surgem sinais reais de bloqueio HTTP
  (`403`, `429` ou `Retry-After`).


## Avaliar pipeline produtor-consumidor por raw

O fluxo atual ja sobrepoe download e persistencia entre fontes quando
`COTACOES_WORKERS` e maior que `1`: os downloads rodam em paralelo e a
persistencia sequencial consome os resultados conforme cada fonte termina.
Ainda assim, o processamento so comeca depois que a fonte conclui o download
completo ou parcial.

A melhoria futura seria trocar esse modelo por uma fila produtor-consumidor por
raw: cada arquivo baixado entraria em uma fila, enquanto um consumidor de
persistencia processaria os raws continuamente sem interromper a raspagem.

No estado atual, os relatorios recentes indicam que o gargalo principal ainda e
o download. A persistencia acumulada ficou baixa em comparacao com a janela de
downloads, entao a mudanca nao parece prioritaria para ganho de tempo agora.

Avaliar essa implementacao somente se os relatorios mostrarem:

- aumento relevante do tempo acumulado de persistencia;
- crescimento do backlog ou da espera na fila;
- processamento de raws virando gargalo em relacao aos downloads;
- necessidade operacional de isolar melhor download e persistencia.

Cuidados antes de implementar:

- garantir que arquivos ainda em escrita nao sejam processados;
- preservar o tratamento de downloads parciais por fonte;
- manter a persistencia SQLite sem escrita concorrente perigosa;
- registrar metricas separadas de fila, processamento e salvamento;
- manter a capacidade de reconstruir o banco a partir dos raws.

## Retomar Supabase no futuro

A integracao com Supabase existe, mas fica fora do escopo operacional por
enquanto. O banco SQLite ficou grande demais para a capacidade esperada do plano
avaliado, entao manter a sincronizacao remota ativa agora tende a trazer mais
custo e instabilidade do que beneficio.

Decisao atual:

- manter o SQLite publicado como artefato principal;
- nao investir agora em correcoes incrementais do Supabase;
- retomar essa frente somente se houver plano/infra compatível com o tamanho da
  base ou necessidade real de API remota;
- se a frente voltar, reavaliar tambem atualizacao de registros antigos ja
  enviados, nao apenas insercao de novos registros.

## Duplicacao de registros e inchaco do SQLite

O banco de dados SQLite cresce indevidamente (~1.03 GB) devido a falhas na constraint de unicidade (`ON CONFLICT (chave_unica) DO NOTHING`).

### Detalhes técnicos:
1. A `chave_unica` de `cotacoes` baseia-se na `coleta_key`.
2. A `coleta_key` usa a coluna `arquivo_raw` (que inclui a data/hora do download no nome do arquivo físico).
3. Downloads repetidos do mesmo arquivo histórico criam novas chaves únicas, inserindo cópias idênticas.

### Ações futuras:
- Desvincular a `chave_unica` de dados físicos/timestamps de download (usar dados lógicos da cotação: data_cotacao, produto, categoria, preços, etc.).
- Limpar os registros duplicados existentes no banco SQLite.
