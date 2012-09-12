from __future__ import absolute_import


import collections, math, logging, numpy as np, pprint, sys, time
from .. import reference
from ..options import options
from ..Worker import WorkerProcess, WorkerThread
from ..ResultCollector import ResultCollectorProcess, ResultCollectorThread
from pbcore.io import rangeQueries, GffWriter
from ..io.fastx import FastaWriter

try:
    import ConsensusCore as cc
    from GenomicConsensus.quiver.utils import *
    from GenomicConsensus.quiver.model import *
    if cc.Version.IsAtLeast(0, 2, 0):
        availability = (True, "OK")
    else:
        availability = (False, "Need ConsensusCore >= 0.2.0")
except ImportError:
    availability = (False, "ConsensusCore not installed---required for Quiver algorithm")


#
# Tuning parameters, here for now
#
POA_COVERAGE    = 11
MUTATION_SEPARATION = 7

#
# The type used in the wire protocol.  Note that whereas plurality
# passes a packet per locus, we pass a bigger packet per reference
# window (1000bp, typically).
#
QuiverWindowSummary = collections.namedtuple("QuiverWindowSummary",
                                             ("referenceId",
                                              "referenceStart",
                                              "referenceEnd",
                                              "consensus",      # string
                                              "variants"))      # list of Variant

def domain(referenceWindow):
    """
    Calculate the "domain" within the referenceWindow---the region
    where we are allowed to call variants, since there is overlap
    between windows.
    """
    refId, winStart, winEnd = referenceWindow
    _, refGroupStart, refGroupEnd = \
        options.referenceWindow or \
        (refId, 0, reference.byId[refId].length)
    domainStart = winStart if winStart==refGroupStart else min(winStart + 5, winEnd)
    domainEnd   = winEnd   if winEnd  ==refGroupEnd   else max(winEnd   - 5, winStart)
    return (domainStart, domainEnd)


class QuiverWorker(object):

    def onStart(self):
        self.params = ParameterSet.fromString(options.parameters)
        self.model  = self.params.model

    def onChunk(self, referenceWindow, alnHits):
        refId, refStart, refEnd = referenceWindow
        domainStart, domainEnd = domain(referenceWindow)
        refSequence = reference.byId[refId].sequence[refStart:refEnd].tostring()

        spanningAlns = [a for a in alnHits
                         if a.spansReferenceRange(refStart, refEnd)]
        # HACK below is to work around bug 21232 until it can be properly fixed
        nonSpanningAlns = [a for a in alnHits
                           if not a.spansReferenceRange(refStart, refEnd)
                           if referenceSpanWithinWindow(referenceWindow, a) > 0]  # <- HACK
        coverageReport = "[span=%d, nonspan=%d]" % (len(spanningAlns), len(nonSpanningAlns))

        # We always prefer spanning reads, and among partial passes,
        # we favor those with a longer read length within the window.
        nonSpanningAlns.sort(key=lambda aln: -referenceSpanWithinWindow(referenceWindow, aln))
        consideredAlns = (spanningAlns + nonSpanningAlns)[:options.coverage]
        siteCoverage = rangeQueries.getCoverageInRange(self._inCmpH5, referenceWindow,
                                                       rowNumbers=[a.rowNumber
                                                                   for a in consideredAlns])
        clippedAlns = [a.clippedTo(refStart, refEnd) for a in consideredAlns]

        # Remove "stumps", where for example the aligner may have
        # inserted a large gap, such that while the alignment
        # technically spans the window, it may not have any read
        # content therein:
        #
        #   Ref   ATGATCCAGTTACTCCGATAAA
        #   Read  ATG---------------TA-A
        #   Win.     [              )
        #
        minimumReadLength = 0.1*(refEnd-refStart)
        clippedAlns = filter(lambda a: a.readLength >= minimumReadLength, clippedAlns)

        # Load the bits that POA cares about.
        forwardStrandSequences = [a.read(orientation="genomic", aligned=False)
                                  for a in clippedAlns
                                  if a.spansReferenceRange(refStart, refEnd)]
        # Load the bits that quiver cares about
        mappedReads = [self.model.extractMappedRead(ca, refStart)
                       for ca in clippedAlns]

        # Beyond this point, no more reference will be made to the
        # pbcore alignment objects.

        # If there are any spanning reads, we can construct a POA
        # consensus as a starting point.  If there are no spanning
        # reads, we use the reference.

        #if True:
        if len(spanningAlns) == 0:
            css = refSequence
            numPoaVariants = None
        else:
            # We calculate a quick consensus estimate using the
            # Partial Order Aligner (POA), which typically gets us to
            # > 99% accuracy.  We have to lift the reference
            # coordinates into coordinates within the POA consensus.
            p = cc.PoaConsensus.FindConsensus(forwardStrandSequences[:POA_COVERAGE])
            ga = cc.Align(refSequence, p.Sequence())
            numPoaVariants = ga.Errors()
            css = p.Sequence()

            # Lift reference coordinates onto the POA consensus coordinates
            targetPositions = cc.TargetToQueryPositions(ga)

            def lifted(targetPositions, mappedRead):
                newStart = targetPositions[mappedRead.TemplateStart]
                newEnd   = targetPositions[mappedRead.TemplateEnd]
                return cc.MappedRead(mappedRead.Features,
                                     mappedRead.Strand,
                                     newStart,
                                     newEnd)
            mappedReads = [lifted(targetPositions, mr) for mr in mappedReads]

        # Load the reads, including QVs, into a MutationScorer, which is a
        # principled and fast way to test potential refinements to our
        # consensus sequence.
        r = cc.SparseSseQvRecursor()
        mms = cc.SparseSseQvMultiReadMutationScorer(r, self.params.qvModelParams, css)
        for mr in mappedReads:
            mms.AddRead(mr)

        # Test mutations, improving the consensus
        css = refineConsensus(mms)

        # Collect variants---only consider those within our domain of control.
        ga = cc.Align(refSequence, css)
        variants = [v for v in variantsFromAlignment(ga, referenceWindow)
                    if domainStart <= v.refStart < domainEnd]
        for v in variants:
            v.coverage = siteCoverage[v.refStart-refStart]
            rawConfidence = np.median([-mms.Score(invMut)
                                        for invMut in inverseMutations(refStart, v)])
            v.confidence = min(93, int(rawConfidence))

        numQuiverVariants = len(variants)

        # Excise the portion of the consensus clipped to the domain
        targetPositions = cc.TargetToQueryPositions(ga)
        cssDomainStart = targetPositions[domainStart-refStart]
        cssDomainEnd   = targetPositions[domainEnd-refStart]
        domainCss = css[cssDomainStart:cssDomainEnd]

        poaReport    = "POA unavailable" if numPoaVariants==None \
                       else ("%d POA variants" % numPoaVariants)
        quiverReport = "%d Quiver variants" % numQuiverVariants
        logging.info("%s: %s, %s %s" %
                     (referenceWindow, poaReport, quiverReport, coverageReport))

        return QuiverWindowSummary(refId, refStart, refEnd, domainCss, variants)


ContigConsensusChunk = collections.namedtuple("ContigConsensusChunk",
                                              ("refStart", "refEnd", "consensus"))

class QuiverResultCollector(object):

    def onStart(self):
        self.allVariants = []
        # this is a map of refId -> [ContigConsensusChunk];
        self.consensusChunks = collections.defaultdict(list)

    def onResult(self, result):
        assert result.consensus != None and isinstance(result.consensus, str)
        self.allVariants += result.variants
        cssChunk = ContigConsensusChunk(result.referenceStart,
                                        result.referenceEnd,
                                        result.consensus)
        self.consensusChunks[result.referenceId].append(cssChunk)

    def onFinish(self):
        # 1. GFF output.
        if options.gffOutputFilename:
            # Dictionary of refId -> [Variant]
            filteredVariantsByRefId = collections.defaultdict(list)
            for v in self.allVariants:
                if (v.confidence > options.variantConfidenceThreshold and
                    v.coverage   > options.variantCoverageThreshold):
                    filteredVariantsByRefId[v.refId].append(v)
            self.writeVariantsGff(options.gffOutputFilename, filteredVariantsByRefId)

        # 2. FASTA output.  Currently unoptimized--will choke hard on
        # very large references.
        if options.fastaOutputFilename:
            with open(options.fastaOutputFilename, "w") as outfile:
                writer = FastaWriter(outfile)
                for refId, unsortedChunks in self.consensusChunks.iteritems():
                    chunks = sorted(unsortedChunks)
                    css = "".join(chunk.consensus for chunk in chunks)
                    quiverHeader = reference.idToHeader(refId) + "|quiver"
                    writer.writeRecord(quiverHeader, css)

    def writeVariantsGff(self, filename, filteredVariantsByRefId):
        writer = GffWriter(options.gffOutputFilename)
        writer.writeMetaData("pacbio-variant-version", "1.4")
        writer.writeMetaData("date", time.ctime())
        writer.writeMetaData("feature-ontology",
                             "http://song.cvs.sourceforge.net/*checkout*/song/ontology/" +
                             "sofa.obo?revision=1.12")
        writer.writeMetaData("source", "GenomicConsensus v0.2.0")
        writer.writeMetaData("source-commandline",  " ".join(sys.argv))

        # Reference groups.
        for id, entry in reference.byId.iteritems():
            writer.writeMetaData("sequence-header", "%s %s" % (entry.name, entry.header))
            writer.writeMetaData("sequence-region", "%s 1 %d" % (entry.name, entry.length))
        for id in reference.byId:
            for v in filteredVariantsByRefId[id]: writer.writeRecord(v.toGffRecord())


#
# Slave process/thread classes
#
class QuiverWorkerProcess(QuiverWorker, WorkerProcess): pass
class QuiverWorkerThread(QuiverWorker, WorkerThread): pass
class QuiverResultCollectorProcess(QuiverResultCollector, ResultCollectorProcess): pass
class QuiverResultCollectorThread(QuiverResultCollector, ResultCollectorThread):  pass


#
# Plugin API
#
__all__ = [ "name",
            "availability",
            "additionalDefaultOptions",
            "compatibilityWithCmpH5",
            "slaveFactories" ]

name = "Quiver"
additionalDefaultOptions = { "referenceChunkOverlap"      : 5,
                             "variantCoverageThreshold"   : 11,
                             "variantConfidenceThreshold" : 20,
                             "coverage"                   : 100,
                             "parameters"                 : "AllQVsModel.trainedParams1" }

def compatibilityWithCmpH5(cmpH5):
    model = ParameterSet.fromString(options.parameters).model
    if model.isCompatibleWithCmpH5(cmpH5):
        return (True, "OK")
    else:
        return (False, "This Quiver parameter set requires QV features not available in this .cmp.h5 file")

def slaveFactories(threaded):
    # By default we use slave processes. The tuple ordering is important.
    if threaded:
        return (QuiverWorkerThread,  QuiverResultCollectorThread)
    else:
        return (QuiverWorkerProcess, QuiverResultCollectorProcess)

