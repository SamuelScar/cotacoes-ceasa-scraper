# Pendencias

## Resumo rapido

| Pendencia | Resumo |
| --- | --- |
| [Paralelismo entre fontes](#paralelismo-entre-fontes) | Executar downloads de fontes independentes em paralelo, mantendo requisicoes internas e persistencia SQLite sequenciais. |
| [Execucao continua como crawler](#execucao-continua-como-crawler) | Avaliar e implementar execucoes periodicas para coletar e persistir somente dados novos. |
| [Desempenho do processamento de raws](#desempenho-do-processamento-de-raws) | Medir e reduzir o custo do processamento, principalmente na extracao de PDFs e nas fontes mais lentas. |
| [Aprimorar sincronizacao incremental com Supabase](#aprimorar-sincronizacao-incremental-com-supabase) | Atualizar no Supabase registros antigos que foram corrigidos no banco local. |

## Paralelismo entre fontes

Executar fontes independentes em paralelo pode reduzir o tempo total. O
paralelismo deve ocorrer somente entre fontes; requisicoes internas e
persistencia SQLite devem continuar sequenciais.

Status: anotado para avaliacao futura. Nao foi implementado nesta rodada porque
as melhorias atuais priorizaram medicoes, cache e reducao de reprocessamento.

Fluxo proposto:

1. Criar uma fila com as fontes configuradas.
2. Executar uma quantidade limitada de fontes simultaneamente.
3. Manter um coletor e uma sessao HTTP isolados por fonte.
4. Salvar cada raw imediatamente, como ocorre atualmente.
5. Agrupar as falhas por fonte sem interromper as demais.
6. Processar e persistir os raws depois do download paralelo.

Exemplo de comando futuro:

```bash
docker compose run --rm baixar --workers 3
docker compose run --rm tudo --workers 3
```

Cuidados necessarios:

- iniciar com poucos workers e tornar o limite configuravel;
- garantir que a saida identifique a fonte em todas as mensagens;
- preservar delay, retry, `Retry-After` e interrupcao por bloqueio em cada
  fonte;
- evitar que duas tarefas escrevam o mesmo raw;
- nao compartilhar coletores ou sessoes HTTP entre threads;
- evitar escritas concorrentes no SQLite, preferindo uma fase unica de
  persistencia depois dos downloads;
- permitir cancelar a execucao e encerrar os workers corretamente;
- medir o impacto antes de aumentar o paralelismo.

O ganho deve vir da espera simultanea por fontes diferentes, sem aumentar
agressivamente as requisicoes para uma mesma CEASA.

## Execucao continua como crawler

Um crawler deve agendar e coordenar chamadas equivalentes ao comando `tudo`,
reutilizando os fluxos existentes para coletar e persistir somente dados novos.

Fluxo proposto:

1. Iniciar o servico e validar configuracao, diretorios e banco.
2. Executar uma rodada de download para as fontes agendadas.
3. Processar os raws baixados e salvar somente registros novos.
4. Registrar o resultado da rodada por fonte.
5. Aguardar o proximo horario configurado.
6. Encerrar de forma controlada ao receber um sinal do container.

Exemplo de configuracao futura:

```env
COTACOES_CRAWLER_INTERVAL_MINUTES=60
COTACOES_CRAWLER_WORKERS=3
```

Exemplo de servico futuro:

```bash
docker compose up crawler
```

Cuidados necessarios:

- impedir que uma nova rodada comece antes da anterior terminar;
- permitir frequencias diferentes para fontes com atualizacoes distintas;
- manter a coleta idempotente para nao duplicar registros;
- persistir o estado minimo necessario para retomar apos reinicio;
- aplicar backoff maior quando uma fonte permanecer indisponivel;
- registrar inicio, fim, duracao, arquivos baixados, registros novos e falhas
  de cada rodada;
- disponibilizar uma verificacao de saude para identificar processo travado;
- encerrar corretamente durante compactacao, download ou persistencia;
- separar erros temporarios de falhas que exigem manutencao do coletor.

Antes de manter um processo Python ativo, deve ser avaliado o agendamento
externo de `docker compose run --rm tudo`. Um crawler dedicado passa a ser
interessante quando forem necessarios intervalos por fonte, retentativas
coordenadas, estado persistente e observabilidade continua.

## Desempenho do processamento de raws

Melhorar o desempenho do processamento de raws, principalmente nas fontes com
grande volume de arquivos e nos parsers de PDF.

O relatorio `data/relatorios/persistencia_20260610_204715_856649.md` mostrou:

- duracao total de `1h53min54s`;
- processamento dos raws: `1h30min34s`, aproximadamente `79,5%` do total;
- persistencia no SQLite: `19min07s`, aproximadamente `16,8%` do total;
- complemento PROHORT: `3min35s`, aproximadamente `3,1%` do total;
- CEASA-PR, CEASA-PE e CEASA-ES concentraram aproximadamente `85%` da
  execucao;
- a CEASA-PR consumiu aproximadamente `50min12s`, principalmente durante o
  processamento de `618` PDFs.

O estudo confirmou que o principal gargalo esta no processamento dos raws. O
parser, principalmente a extracao e interpretacao dos PDFs, provavelmente
representa uma parte relevante desse custo, mas o fluxo deve ser medido por
etapa antes das otimizacoes.

Medicoes necessarias:

- listagem e leitura dos arquivos;
- extracao de texto dos PDFs;
- parser especifico de cada fonte;
- normalizacao e criacao das cotacoes;
- persistencia no SQLite.

Melhorias a avaliar:

- acompanhar as novas metricas de tempo por etapa nos relatorios;
- acompanhar o ganho do salto de raws ja processados sem alteracao;
- acompanhar o ganho do cache de texto extraido de PDFs;
- processar raws independentes em paralelo com limite configuravel;
- reduzir trabalho repetido dentro dos parsers;
- priorizar a investigacao dos PDFs da CEASA-PR;
- ampliar a instrumentacao se alguma etapa continuar sem granularidade suficiente.

Qualquer otimizacao deve preservar os resultados atuais dos parsers, a
proveniencia dos registros e a capacidade de reconstruir o banco a partir dos
raws.

## Aprimorar sincronizacao incremental com Supabase

Atualmente, a sincronizacao incremental envia registros novos e atualiza as
tabelas pequenas de referencia. Porem, ela nao detecta correcoes feitas em
coletas ou cotacoes que ja haviam sido enviadas ao Supabase.

Exemplo: se o preco de uma cotacao antiga for corrigido no SQLite local, a
sincronizacao incremental nao atualiza essa cotacao no Supabase. Atualmente, e
necessario executar a substituicao completa para enviar a correcao.

A sincronizacao incremental deve identificar e atualizar esses registros
antigos sem precisar substituir todo o banco remoto.
