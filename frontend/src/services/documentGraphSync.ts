import type { GraphData } from '../store/graphStore';
import { executeQuery } from './graphService';

type DocumentGraphQueryResult = {
  graphData: GraphData;
  query: string;
};

type FocusGraphInput = {
  chunkIds: string[];
  entityNames: string[];
  keywords: string[];
};

const DOCUMENT_GRAPH_QUERIES = {
  entityOverview: `MATCH (e1:Entity)-[r]->(e2:Entity)
WHERE r.source = 'document_ingest'
RETURN e1 AS n, r, e2 AS m
LIMIT 600`,
  fallbackOverview: `MATCH (d:Document {source: 'document_ingest'})-[h:HAS_CHUNK]->(c:Chunk)
OPTIONAL MATCH (c)-[m:MENTIONS]->(e:Entity)
RETURN d, h, c, m, e
LIMIT 600`,
  citationFocus: `MATCH (d:Document)-[h:HAS_CHUNK]->(c:Chunk)
WHERE d.source = 'document_ingest'
  AND c.chunk_id IN $chunkIds
MATCH (c)-[m:MENTIONS]->(e:Entity)
WHERE size($keywords) = 0
   OR toLower(e.name) IN $entityNames
   OR any(k IN $keywords WHERE toLower(e.name) CONTAINS k OR k CONTAINS toLower(e.name))
WITH DISTINCT d, h, c, m, e
OPTIONAL MATCH (c)-[:MENTIONS]->(ePeer:Entity)
WHERE ePeer <> e
  AND (
    size($keywords) = 0
    OR toLower(ePeer.name) IN $entityNames
    OR any(k IN $keywords WHERE toLower(ePeer.name) CONTAINS k OR k CONTAINS toLower(ePeer.name))
  )
OPTIONAL MATCH (e)-[r]-(ePeer)
WHERE r.source = 'document_ingest'
RETURN d, h, c, m, e, r, ePeer
LIMIT 240`,
  entityFocus: `MATCH (e:Entity)
WHERE toLower(e.name) IN $entityNames
   OR (
     size($keywords) > 0
     AND any(k IN $keywords WHERE toLower(e.name) CONTAINS k OR k CONTAINS toLower(e.name))
   )
OPTIONAL MATCH (c:Chunk)-[m:MENTIONS]->(e)
OPTIONAL MATCH (d:Document)-[h:HAS_CHUNK]->(c)
WHERE d.source = 'document_ingest'
RETURN d, h, c, m, e
LIMIT 180`,
};

function isEmptyGraph(graphData: GraphData | null) {
  return !graphData || (!graphData.nodes.length && !graphData.edges.length);
}

export async function loadDocumentOverviewGraph(
  kbId?: string,
  isCurrent: () => boolean = () => true,
): Promise<DocumentGraphQueryResult | null> {
  if (!isCurrent()) return null;
  let graphData = await executeQuery(DOCUMENT_GRAPH_QUERIES.entityOverview, undefined, kbId);
  if (!isCurrent()) return null;
  let query = DOCUMENT_GRAPH_QUERIES.entityOverview;

  if (!graphData.edges.length) {
    if (!isCurrent()) return null;
    graphData = await executeQuery(DOCUMENT_GRAPH_QUERIES.fallbackOverview, undefined, kbId);
    if (!isCurrent()) return null;
    query = DOCUMENT_GRAPH_QUERIES.fallbackOverview;
  }

  return { graphData, query };
}

export async function loadFocusedDocumentGraph(
  { chunkIds, entityNames, keywords }: FocusGraphInput,
  kbId?: string,
  isCurrent: () => boolean = () => true,
): Promise<DocumentGraphQueryResult | null> {
  if (!isCurrent()) return null;
  if (chunkIds.length > 0) {
    const graphData = await executeQuery(DOCUMENT_GRAPH_QUERIES.citationFocus, {
      chunkIds,
      entityNames,
      keywords,
    }, kbId);
    if (!isCurrent()) return null;
    if (!isEmptyGraph(graphData)) {
      return { graphData, query: DOCUMENT_GRAPH_QUERIES.citationFocus };
    }
  }

  if (entityNames.length > 0) {
    if (!isCurrent()) return null;
    const graphData = await executeQuery(DOCUMENT_GRAPH_QUERIES.entityFocus, {
      entityNames,
      keywords,
    }, kbId);
    if (!isCurrent()) return null;
    if (!isEmptyGraph(graphData)) {
      return { graphData, query: DOCUMENT_GRAPH_QUERIES.entityFocus };
    }
  }

  return loadDocumentOverviewGraph(kbId, isCurrent);
}
