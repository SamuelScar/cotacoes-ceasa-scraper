# Pendencias

## Resumo rapido

| Pendencia | Resumo |
| --- | --- |
| [Pipeline de download e persistencia](#pipeline-de-download-e-persistencia) | Processar e salvar uma fonte assim que seu download terminar, enquanto outras fontes continuam baixando. |
| [Execucao continua como crawler](#execucao-continua-como-crawler) | Avaliar e implementar execucoes periodicas para coletar e persistir somente dados novos. |
| [Desempenho do processamento de raws](#desempenho-do-processamento-de-raws) | Medir e reduzir o custo do processamento, principalmente na extracao de PDFs e nas fontes mais lentas. |
| [Aprimorar sincronizacao incremental com Supabase](#aprimorar-sincronizacao-incremental-com-supabase) | Atualizar no Supabase registros antigos que foram corrigidos no banco local. |
| [Migrar documentacao extensa para GitHub Wiki](#migrar-documentacao-extensa-para-github-wiki) | Reorganizar guias longos na Wiki quando o repositorio puder ficar publico. |

## Pipeline de download e persistencia

Depois do paralelismo de download entre fontes, avaliar uma evolucao em pipeline
para o comando `tudo`. Nesse fluxo, uma fonte que terminou o download entra em
uma fila de processamento enquanto outras fontes continuam baixando.

Contexto da etapa ja implementada:
[estudo-paralelismo.md](estudo-paralelismo.md).

Fluxo futuro:

1. Baixar fontes em paralelo com limite de workers.
2. Enviar cada fonte concluida para uma fila de persistencia.
3. Manter apenas um consumidor processando raws e salvando no SQLite.
4. Preservar os arquivos parciais quando uma fonte falhar durante o download.
5. Consolidar o relatorio sem misturar eventos de download e persistencia.

Exemplo conceitual:

```text
downloads paralelos:
  worker 1 baixa ceasa-pe
  worker 2 baixa ceasa-mg
  worker 3 baixa ceasa-pr

fila de persistencia:
  ceasa-mg terminou -> processa/salva ceasa-mg
  ceasa-pe terminou -> processa/salva ceasa-pe
  ceasa-pr terminou -> processa/salva ceasa-pr
```

Cuidados necessarios:

- nao permitir mais de uma escrita simultanea no SQLite;
- separar claramente logs de download e logs de persistencia;
- cancelar downloads e esvaziar a fila de forma controlada em interrupcoes;
- definir como ordenar o relatorio quando download e persistencia ocorrerem ao
  mesmo tempo;
- medir se o ganho adicional compensa a complexidade.

Essa pendencia deve vir depois do paralelismo simples de download, porque depende
de resultados estruturados por fonte, controle de workers e tratamento confiavel
de falhas parciais.

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

## Migrar documentacao extensa para GitHub Wiki

Quando o repositorio puder ficar publico, avaliar a criacao da Wiki do GitHub
para concentrar a documentacao longa do projeto.

Objetivo:

1. Manter no repositorio apenas a documentacao essencial para rodar e manter o
   codigo.
2. Mover guias longos, estudos tecnicos, roadmap e detalhes operacionais para a
   Wiki.
3. Usar o `README.md` como porta de entrada, com links para as paginas da Wiki.

Conteudos candidatos para a Wiki:

- comandos de operacao detalhados;
- fluxo de coleta;
- fontes e limitacoes;
- modelo de dados;
- pacote `data.tar.gz`;
- sincronizacao com Supabase;
- estrategias anti-bloqueio;
- decisoes tecnicas com contexto;
- pendencias, roadmap e estudos tecnicos.

Cuidados:

- manter `.env.example` no repositorio, porque acompanha o comportamento real do
  codigo;
- manter no repositorio configuracoes, comandos criticos e decisoes que precisam
  mudar junto com o codigo;
- evitar que a Wiki vire a unica fonte de informacoes necessarias para executar
  o projeto localmente.
